from __future__ import annotations

import asyncio
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
    try:
        from bianbu_agent_sdk.memory import get_memory_bridge
        config = run.get("config", {})
        account, agent = _memory_identity(run)
        bridge = await get_memory_bridge(socket_path=config.get("socket_path"),
            timeout=config.get("timeout_seconds", 2.0), account_id=account, agent_name=agent)
        return await asyncio.wait_for(bridge.record_runtime_result(
            run_id=run["run_id"], session_id=run["session_id"], agent_id=run["agent_id"],
            user_text=run["request"], assistant_text=run["response"], status=run["status"],
            metadata=run.get("metadata", {}), config=config),
            timeout=config.get("timeout_seconds", 2.0))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("AgentMemory turn recording unavailable", exc_info=True)
        return False


async def after_run(run: dict) -> dict | None:
    if memory_plugin_selected(run) is False:
        return None
    if _agent_memory_enabled():
        await _record_agent_memory(run)
        return None
    try:
        from bianbu_agent_sdk.memory.lifecycle import record_legacy_result
        return record_legacy_result(os.environ["PLUGIN_DATA_ROOT"], run)
    except Exception:
        logger.warning("Legacy memory recording unavailable", exc_info=True)
        return None


def _warmup():
    from bianbu_agent_sdk.memory import get_memory_bridge  # noqa: F401
    from bianbu_agent_sdk.memory import lifecycle  # noqa: F401


if __name__ == "__main__":
    from protocol import main

    main(after_run, warmup=_warmup)
