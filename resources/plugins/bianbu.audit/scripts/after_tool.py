from __future__ import annotations

from pathlib import Path

from collections.abc import Mapping
import hashlib
import json
import logging
import os


logger = logging.getLogger(__name__)

_MEMORY_HOOK_IDS = frozenset(
    {
        "bianbu.memory/memory.before_run",
        "bianbu.memory/memory.after_run",
    }
)


def _agent_memory_enabled():
    return os.environ.get("BIANBU_AGENT_MEMORY_ENABLED", "true").strip().casefold() not in {
        "0", "false", "no", "off",
    }


def _memory_plugin_selected(run):
    raw = run["metadata"].get("hook_ids")
    if raw is None:
        return True
    if isinstance(raw, (str, bytes, bytearray)):
        selected = {str(raw)}
    else:
        try:
            selected = {str(item) for item in raw}
        except TypeError:
            selected = set()
    return "*" in selected or _MEMORY_HOOK_IDS.issubset(selected)


def _operation_details(run):
    raw = run["metadata"].get("arguments")
    arguments = dict(raw) if isinstance(raw, Mapping) else {}
    command = next(
        (
            str(arguments[key]).strip()
            for key in ("command", "cmd")
            if isinstance(arguments.get(key), str) and str(arguments[key]).strip()
        ),
        None,
    )
    files = []
    for key in ("path", "file", "file_path", "files", "paths"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            files.append(value.strip())
        elif isinstance(value, (list, tuple)):
            files.extend(str(item).strip() for item in value if str(item).strip())
    canonical = json.dumps(
        {"tool": str(run["metadata"].get("tool") or run["request"]), "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    operation_id = str(
        run["metadata"].get("call_id")
        or run["metadata"].get("tool_call_id")
        or hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
    return command, files, operation_id


async def _record_agent_memory(run, kind):
    if not _agent_memory_enabled() or not _memory_plugin_selected(run):
        return
    try:
        from bianbu_agent_sdk.memory import AsyncMemoryClient

        socket_path = os.environ.get("BIANBU_AGENT_MEMORY_SOCKET") or "/run/bianbu/agent-memoryd.sock"
        account_id = str(run["account_id"])
        agent_name = str(run["agent_id"])
        client = await AsyncMemoryClient.connect(
            socket_path,
            timeout=2.0,
            account_id=account_id,
            agent_name=agent_name or str(run["agent_id"]),
        )
        try:
            details = {
                "run_id": run["run_id"],
                "session_id": run["session_id"],
                "agent_id": run["agent_id"],
                "tool": str(run["metadata"].get("tool") or ""),
            }
            await client.audit.record(
                kind,
                result="success",
                details=details,
            )
            from bianbu_agent_sdk.memory import get_memory_bridge

            command, files, operation_id = _operation_details(run)
            bridge = await get_memory_bridge(
                timeout=2.0,
                account_id=account_id,
                agent_name=agent_name,
            )
            await bridge.record_operation_event(
                run_id=run["run_id"],
                session_id=run["session_id"],
                agent_id=run["agent_id"],
                event_type="tool_result",
                tool=str(run["metadata"].get("tool") or run["request"]),
                status="completed",
                command=command,
                files=files or None,
                operation_id=operation_id,
            )
        finally:
            await client.close()
    except Exception as exc:
        logger.warning("AgentMemory audit write unavailable: %s", exc)


async def after_tool(run: dict) -> dict | None:
    Path(os.environ["PLUGIN_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
    path = Path(os.environ["PLUGIN_DATA_ROOT"]) / "audit.jsonl"
    payload = {
        "kind": "tool.completed",
        "run_id": run["run_id"],
        "session_id": run["session_id"],
        "agent_id": run["agent_id"],
        "tool": str(run["metadata"].get("tool") or ""),
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    await _record_agent_memory(run, "tool.completed")
    return None


if __name__ == "__main__":
    from protocol import main

    main(after_tool)
