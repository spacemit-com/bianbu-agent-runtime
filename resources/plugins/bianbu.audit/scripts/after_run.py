from __future__ import annotations

from pathlib import Path

import json
import logging
import os


logger = logging.getLogger(__name__)


def _agent_memory_enabled():
    return os.environ.get("BIANBU_AGENT_MEMORY_ENABLED", "true").strip().casefold() not in {
        "0", "false", "no", "off",
    }


async def _record_agent_memory(run, kind):
    if not _agent_memory_enabled():
        return
    try:
        from bianbu_agent_sdk.memory import AsyncMemoryClient

        socket_path = os.environ.get("BIANBU_AGENT_MEMORY_SOCKET") or "/run/bianbu/agent-memoryd.sock"
        account_id = os.environ.get(
            "BIANBU_AGENT_MEMORY_ACCOUNT_ID", "default"
        ).strip() or "default"
        agent_name = os.environ.get("BIANBU_AGENT_MEMORY_AGENT_NAME", "").strip()
        client = await AsyncMemoryClient.connect(
            socket_path,
            timeout=2.0,
            account_id=account_id,
            agent_name=agent_name or str(run["agent_id"]),
        )
        try:
            await client.audit.record(
                kind,
                result=run["status"] or "completed",
                details={
                    "run_id": run["run_id"],
                    "session_id": run["session_id"],
                    "agent_id": run["agent_id"],
                },
            )
        finally:
            await client.close()
    except Exception as exc:
        logger.warning("AgentMemory audit write unavailable: %s", exc)


def _append(run, kind):
    Path(os.environ["PLUGIN_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
    path = Path(os.environ["PLUGIN_DATA_ROOT"]) / "audit.jsonl"
    payload = {"kind": kind, "run_id": run["run_id"], "session_id": run["session_id"], "agent_id": run["agent_id"]}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


async def after_run(run: dict) -> dict | None:
    _append(run, "run.completed")
    await _record_agent_memory(run, "run.completed")
    return None


if __name__ == "__main__":
    from protocol import main

    main(after_run)
