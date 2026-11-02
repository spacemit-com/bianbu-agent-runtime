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
    """Return the role-level memory switch represented by selected Hooks.

    ``None`` means the caller did not provide a Hook snapshot (for example a
    direct unit-test invocation).  In that case the environment switch keeps
    its backwards-compatible meaning.  An explicit empty snapshot means the
    role has removed both memory Hooks and the complete memory capability is
    disabled, including the memory Tools.
    """

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
    """Return the explicit backend switch (on by default)."""

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

async def _record_run_started_watermark(run) -> None:
    """Finish the best-effort watermark before the command process exits."""
    if run["metadata"].get("console_operation") == "generate_title":
        return
    try:
        from bianbu_agent_sdk.memory import get_memory_bridge

        account_id, agent_name = _memory_identity(run)
        config = run.get("config", {})
        bridge = await get_memory_bridge(
            socket_path=config.get("socket_path"),
            timeout=config.get("timeout_seconds", 2.0),
            account_id=account_id,
            agent_name=agent_name,
        )
        record = getattr(bridge, "record_run_started", None)
        if not callable(record):
            return
        await asyncio.wait_for(
            record(
                run_id=run["run_id"],
                session_id=run["session_id"],
                agent_id=run["agent_id"],
                user_text=run["request"],
            ),
            timeout=config.get("timeout_seconds", 2.0),
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # best-effort watermark; never block or fail the run
        logger.debug("run-start watermark unavailable", exc_info=True)

async def _context_for_run(run: dict) -> dict | None:
    if memory_plugin_selected(run) is False:
        return None
    if _agent_memory_enabled():
        try:
            from bianbu_agent_sdk.memory import get_memory_bridge
            account, agent = _memory_identity(run)
            config = run.get("config", {})
            bridge = await get_memory_bridge(socket_path=config.get("socket_path"),
                timeout=config.get("timeout_seconds", 2.0), account_id=account, agent_name=agent)
            return await asyncio.wait_for(bridge.prepare_context(
                query=run["request"], session_id=run["session_id"], config=config),
                timeout=config.get("timeout_seconds", 2.0))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("AgentMemory context unavailable", exc_info=True)
            return None
    try:
        from bianbu_agent_sdk.memory.context import legacy_context
        return legacy_context(os.environ["PLUGIN_DATA_ROOT"])
    except Exception:
        logger.warning("Legacy memory context unavailable", exc_info=True)
        return None


async def before_run(run: dict) -> dict | None:
    if memory_plugin_selected(run) is False:
        return None
    if not _agent_memory_enabled():
        return await _context_for_run(run)
    patch, _ = await asyncio.gather(
        _context_for_run(run), _record_run_started_watermark(run),
    )
    return patch


def _warmup():
    from bianbu_agent_sdk.memory import get_memory_bridge  # noqa: F401
    from bianbu_agent_sdk.memory import context  # noqa: F401


if __name__ == "__main__":
    from protocol import main

    main(before_run, warmup=_warmup)
