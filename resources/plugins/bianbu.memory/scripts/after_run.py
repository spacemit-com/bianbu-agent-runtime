from __future__ import annotations

from pathlib import Path

import asyncio
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


def memory_plugin_selected(run) -> bool | None:
    """Return the role-level memory switch represented by selected Hooks."""

    raw = run.get("metadata", {}).get("hook_ids")
    if raw is None:
        return None
    if isinstance(raw, (str, bytes, bytearray)):
        selected = {str(raw)}
    else:
        try:
            selected = {str(item) for item in raw}
        except TypeError:
            selected = set()
    if "*" in selected:
        return True
    return _MEMORY_HOOK_IDS.issubset(selected)


def _agent_memory_enabled() -> bool:
    value = os.environ.get("BIANBU_AGENT_MEMORY_ENABLED")
    if value is None or not value.strip():
        return True
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logger.warning(
        "invalid BIANBU_AGENT_MEMORY_ENABLED value; defaulting to enabled"
    )
    return True


def _memory_identity(run) -> tuple[str, str | None]:
    # Identity is trusted Runtime call context, never process environment.
    return str(run["account_id"]), str(run["agent_id"])


async def _record_agent_memory(run) -> bool:
    if run["metadata"].get("console_operation") == "generate_title":
        return False
    try:
        from bianbu_agent_sdk.memory import get_memory_bridge

        config = run.get("config", {})
        account_id, agent_name = _memory_identity(run)
        bridge = await get_memory_bridge(
            socket_path=config.get("socket_path"),
            timeout=config.get("timeout_seconds", 2.0),
            account_id=account_id,
            agent_name=agent_name,
        )
        if run["status"] == "completed" and run["response"].strip():
            return await asyncio.wait_for(
                bridge.record_turn(
                    run_id=run["run_id"],
                    session_id=run["session_id"],
                    agent_id=run["agent_id"],
                    user_text=run["request"],
                    assistant_text=run["response"],
                ),
                timeout=config.get("timeout_seconds", 2.0),
            )
        record_interrupted = getattr(bridge, "record_interrupted_turn", None)
        if not callable(record_interrupted):
            return False
        return await asyncio.wait_for(
            record_interrupted(
                run_id=run["run_id"],
                session_id=run["session_id"],
                agent_id=run["agent_id"],
                user_text=run["request"],
                status=run["status"] or "interrupted",
                error=str(run["metadata"].get("error") or "").strip() or None,
            ),
            timeout=config.get("timeout_seconds", 2.0),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # memory is best-effort and must not fail the run
        logger.warning("AgentMemory turn recording unavailable: %s", exc)
        return False


async def after_run(run: dict) -> dict | None:
    if memory_plugin_selected(run) is False:
        return None
    if _agent_memory_enabled():
        await _record_agent_memory(run)
        return None
    content = str(run["metadata"].get("memory_to_record") or "").strip()
    if not content:
        return None
    Path(os.environ["PLUGIN_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
    path = Path(os.environ["PLUGIN_DATA_ROOT"]) / "memory.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"content": content}, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return None


if __name__ == "__main__":
    from protocol import main

    main(after_run)
