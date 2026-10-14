from __future__ import annotations

from pathlib import Path

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
            await client.audit.record(
                kind,
                result=run["status"] or "failed",
                details={
                    "run_id": run["run_id"],
                    "session_id": run["session_id"],
                    "agent_id": run["agent_id"],
                    "error": str(run["metadata"].get("error") or "")[:2000],
                },
            )
            from bianbu_agent_sdk.memory import get_memory_bridge

            error = str(run["metadata"].get("error") or "")[:2000]
            operation_id = hashlib.sha256(
                f"{run["run_id"]}\0{run["metadata"].get('tool') or 'run'}\0{error}".encode("utf-8")
            ).hexdigest()
            bridge = await get_memory_bridge(
                timeout=2.0,
                account_id=account_id,
                agent_name=agent_name,
            )
            await bridge.record_operation_event(
                run_id=run["run_id"],
                session_id=run["session_id"],
                agent_id=run["agent_id"],
                event_type="task_failed",
                tool=str(run["metadata"].get("tool") or "run"),
                status="failed",
                error=error or None,
                operation_id=operation_id,
            )
        finally:
            await client.close()
    except Exception as exc:
        logger.warning("AgentMemory audit write unavailable: %s", exc)


async def on_error(run: dict) -> dict | None:
    Path(os.environ["PLUGIN_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
    path = Path(os.environ["PLUGIN_DATA_ROOT"]) / "audit.jsonl"
    payload = {"kind": "run.failed", "run_id": run["run_id"], "session_id": run["session_id"], "agent_id": run["agent_id"]}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    await _record_agent_memory(run, "run.failed")
    return None


if __name__ == "__main__":
    from protocol import main

    main(on_error)
