from __future__ import annotations

from pathlib import Path
import asyncio
import logging

import pytest

from tests.sdk.api.test_runs import _api
from bianbu_agent.observability import bind_log_context


@pytest.mark.asyncio
async def test_agent_top_aggregates_agents_sessions_and_runs(tmp_path: Path) -> None:
    """The monitor returns one bounded, prompt-free operational snapshot."""

    async with _api(tmp_path) as (client, runtime):
        session = await client.post(
            "/api/v1/sessions",
            json={"agent_id": "writer", "title": "Draft release notes"},
        )
        session_id = session.json()["data"]["id"]
        created = await client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"message_id": "monitor-message", "content": "private prompt"},
        )
        run_id = created.json()["data"]["run_id"]
        for _ in range(100):
            record = await runtime.runs.get_record(run_id)
            if record is not None and record.status == "completed":
                break
            await asyncio.sleep(0.01)

        response = await client.get("/api/v1/agent-top")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"] == {
        "agents": 2,
        "available_agents": 0,
        "sessions": 1,
        "runs": 1,
        "active_runs": 0,
        "waiting_runs": 0,
        "failed_runs": 0,
    }
    writer = next(item for item in data["agents"] if item["id"] == "writer")
    assert writer["session_count"] == 1
    assert writer["run_count"] == 1
    assert writer["status"] == "unavailable"
    assert data["recent_runs"][0]["session_title"] == "Draft release notes"
    assert data["recent_runs"][0]["status"] == "completed"
    assert "private prompt" not in response.text
    assert len(data["recent_runs"]) <= 50


@pytest.mark.asyncio
async def test_operational_logs_api_filters_without_exposing_other_runs(tmp_path: Path) -> None:
    async with _api(tmp_path) as (client, runtime):
        logger = logging.getLogger("bianbu_agent.test.monitoring")
        with bind_log_context(
            agent_id="writer", session_id="session-a", run_id="run-a"
        ):
            logger.info("selected")
        with bind_log_context(
            agent_id="reviewer", session_id="session-b", run_id="run-b"
        ):
            logger.info("not selected")

        response = await client.get(
            "/api/v1/operational-logs", params={"run_id": "run-a"}
        )

    assert response.status_code == 200
    records = response.json()["data"]
    assert [record["message"] for record in records] == ["selected"]
    assert records[0]["agent_id"] == "writer"
    assert "not selected" not in response.text


@pytest.mark.asyncio
async def test_completed_run_has_one_correlated_terminal_log(tmp_path: Path) -> None:
    async with _api(tmp_path) as (client, runtime):
        session = await client.post(
            "/api/v1/sessions", json={"agent_id": "writer", "title": "Logs"}
        )
        session_id = session.json()["data"]["id"]
        created = await client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"message_id": "log-message", "content": "hello"},
        )
        run_id = created.json()["data"]["run_id"]
        for _ in range(100):
            record = await runtime.runs.get_record(run_id)
            if record is not None and record.status == "completed":
                break
            await asyncio.sleep(0.01)

        response = None
        for _ in range(100):
            response = await client.get(
                "/api/v1/operational-logs", params={"run_id": run_id}
            )
            if any(
                item.get("event") == "run_terminal"
                for item in response.json()["data"]
            ):
                break
            await asyncio.sleep(0.01)

    assert response is not None
    terminals = [
        record for record in response.json()["data"]
        if record.get("event") == "run_terminal"
    ]
    assert len(terminals) == 1
    assert terminals[0]["agent_id"] == "writer"
    assert terminals[0]["session_id"] == session_id
    assert terminals[0]["plugin_generation"] == 1
    assert terminals[0]["run_status"] == "completed"
