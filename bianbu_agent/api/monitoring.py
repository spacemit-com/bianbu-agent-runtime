"""Operational Agent and Run snapshot for the Console monitor."""

from __future__ import annotations

import time
from collections import Counter

from fastapi import APIRouter, Query, Request

from bianbu_agent.api.agents import _availability
from bianbu_agent.api.contracts import (
    AgentTopEnvelope,
    OperationalLogCollectionEnvelope,
    OperationalLogView,
)
from bianbu_agent.api.runs import _pending_actions


router = APIRouter()
_TERMINAL = {"completed", "failed", "cancelled"}


@router.get("/operational-logs", response_model=OperationalLogCollectionEnvelope)
async def operational_logs(
    request: Request,
    agent_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=10_000),
) -> OperationalLogCollectionEnvelope:
    records = request.app.state.runtime.runtime_log_store.query(
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        limit=limit,
    )
    return OperationalLogCollectionEnvelope(
        data=[OperationalLogView.model_validate(record) for record in records]
    )


def _run_status(record: object) -> str:
    status = str(getattr(record, "status", "queued"))
    if status not in _TERMINAL and _pending_actions(getattr(record, "events", ())):
        return "waiting"
    return status


@router.get("/agent-top", response_model=AgentTopEnvelope)
async def agent_top(request: Request) -> AgentTopEnvelope:
    runtime = request.app.state.runtime
    agents = await runtime.agents.list()
    sessions = await runtime.sessions.list()
    runs = [
        run
        for run in await runtime.runs.list()
        if dict(getattr(run, "request", {}) or {}).get("metadata", {}).get(
            "console_operation"
        )
        != "generate_title"
    ]
    providers = {
        item.id: item for item in await runtime.model_configs.list_providers()
    }
    sessions_by_id = {item.id: item for item in sessions}
    session_counts = Counter(item.agent_id for item in sessions)
    run_counts: Counter[str] = Counter()
    active_counts: Counter[str] = Counter()
    failed_counts: Counter[str] = Counter()
    last_active: dict[str, float] = {
        item.id: max(
            (float(session.updated_at) for session in sessions if session.agent_id == item.id),
            default=0.0,
        )
        for item in agents
    }
    statuses: dict[str, str] = {}
    for run in runs:
        agent_id = str(dict(run.request).get("agent_id") or "")
        status = _run_status(run)
        statuses[run.request_id] = status
        run_counts[agent_id] += 1
        if status not in _TERMINAL:
            active_counts[agent_id] += 1
        if status == "failed":
            failed_counts[agent_id] += 1
        last_active[agent_id] = max(last_active.get(agent_id, 0.0), float(run.updated_at))

    agent_rows = []
    for agent in agents:
        available = bool(_availability(agent, providers)["available"])
        agent_statuses = [
            statuses[run.request_id]
            for run in runs
            if str(dict(run.request).get("agent_id") or "") == agent.id
            and statuses[run.request_id] not in _TERMINAL
        ]
        status = (
            "unavailable"
            if not available
            else "waiting"
            if "waiting" in agent_statuses
            else "running"
            if agent_statuses
            else "idle"
        )
        avatar = str(agent.metadata.get("avatar") or "🤖")
        try:
            if runtime.agent_packages.avatar_path(agent.id) is not None:
                avatar = f"/api/v1/agents/{agent.id}/avatar"
        except KeyError:
            pass
        agent_rows.append(
            {
                "id": agent.id,
                "name": agent.name,
                "name_zh": agent.name_zh,
                "name_en": agent.name_en,
                "avatar": avatar,
                "available": available,
                "status": status,
                "session_count": session_counts[agent.id],
                "run_count": run_counts[agent.id],
                "active_run_count": active_counts[agent.id],
                "failed_run_count": failed_counts[agent.id],
                "last_active_at": last_active.get(agent.id, 0.0),
            }
        )
    agent_rows.sort(
        key=lambda item: (item["active_run_count"], item["last_active_at"], item["id"]),
        reverse=True,
    )

    recent = []
    for run in sorted(runs, key=lambda item: (item.updated_at, item.request_id), reverse=True)[:50]:
        session = sessions_by_id.get(run.session_id)
        recent.append(
            {
                "id": run.request_id,
                "session_id": run.session_id,
                "session_title": str(getattr(session, "title", "")),
                "agent_id": str(dict(run.request).get("agent_id") or ""),
                "status": statuses[run.request_id],
                "created_at": float(run.created_at),
                "updated_at": float(run.updated_at),
            }
        )

    active_runs = sum(status not in _TERMINAL for status in statuses.values())
    return AgentTopEnvelope(
        data={
            "summary": {
                "agents": len(agents),
                "available_agents": sum(bool(row["available"]) for row in agent_rows),
                "sessions": len(sessions),
                "runs": len(runs),
                "active_runs": active_runs,
                "waiting_runs": sum(status == "waiting" for status in statuses.values()),
                "failed_runs": sum(status == "failed" for status in statuses.values()),
            },
            "agents": agent_rows,
            "recent_runs": recent,
            "server_time": time.time(),
        }
    )


__all__ = ["router"]
