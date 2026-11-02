from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from .events import RunEvent


def _base_url() -> str:
    base = os.environ.get("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("Time confirmation requires the local Bibit service")
    return base


async def _complete(request: Mapping[str, object], session_id: str, decision: str) -> dict:
    base = _base_url()
    async with httpx.AsyncClient(trust_env=False, timeout=40.0) as client:
        response = await client.post(base + "/v1/settings/time/confirmation",
                                     json={"id": request["id"], "session_id": session_id, "decision": decision},
                                     headers={"X-Bibit-Confirmation-Token": str(request["token"])})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool or not isinstance(payload.get("reply"), str):
            raise ValueError("Invalid time confirmation result")
        return payload


async def _prepare_cloud(target: str, session_id: str, run_id: str) -> dict:
    async with httpx.AsyncClient(trust_env=False, timeout=10.0) as client:
        response = await client.post(_base_url() + "/v1/settings/time/prepare",
                                     json={"datetime": target, "session_id": session_id, "run_id": run_id})
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("kind") != "disable_ntp_and_set_time":
        raise ValueError("Invalid time confirmation ticket")
    if not all(isinstance(payload.get(key), str) and payload[key]
               for key in ("id", "token", "datetime", "description")):
        raise ValueError("Incomplete time confirmation ticket")
    return payload


async def resolve_cloud_time_confirmation(result, *, target, context, control):
    from bianbu_agent.plugins.contributions import ToolResult
    from bianbu_agent.runs.control import RunControl

    try:
        payload = json.loads(result.content)
    except (ValueError, TypeError):
        return result
    request = payload.get("confirmation") if isinstance(payload, dict) else None
    if request is None:
        return result
    outcome = {"ok": False, "reply": "时间确认未完成，未继续设置时间。"}
    ticket = None
    submitted = False
    try:
        if not isinstance(request, dict) or request.get("kind") != "disable_ntp_and_set_time":
            raise ValueError("Invalid time confirmation")
        if not isinstance(control, RunControl) or control.cancelled.is_set():
            raise ValueError("Time confirmation requires a live Run")
        if not isinstance(target, str) or not target:
            raise ValueError("Invalid time target")
        ticket = await _prepare_cloud(target, context.session_id, context.run_id)
        if ticket["datetime"] != request.get("datetime"):
            raise ValueError("Time confirmation target changed")
        decisions = await control.request_approval({
            "approval_id": uuid.uuid4().hex,
            "kind": "disable_ntp_and_set_time",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=150)).isoformat(),
            "title": "关闭自动校时并设置时间",
            "description": ticket["description"],
            "actions": [{"kind": "system", "name": "disable_ntp_and_set_time",
                         "arguments": {"datetime": ticket["datetime"]},
                         "description": ticket["description"],
                         "allowed_decisions": ["approve", "reject"]}],
        })
        approved = (len(decisions) == 1 and decisions[0].get("type") == "approve"
                    and not control.cancelled.is_set())
        submitted = True
        outcome = await _complete(ticket, context.session_id, "approve" if approved else "reject")
    except Exception:
        outcome = {"ok": False, "reply": "时间操作结果未确认，请检查系统时间和自动校时状态；本次不会自动重试。"}
    finally:
        if ticket is not None and not submitted:
            try:
                await _complete(ticket, context.session_id, "reject")
            except Exception:
                pass
    return ToolResult(content=json.dumps({"ok": outcome["ok"], "message": outcome["reply"],
                                          "detail": outcome.get("detail", "")}, ensure_ascii=False),
                      metadata={**dict(result.metadata), "mcp_error": not outcome["ok"]})


async def resolve_time_confirmation(request, *, handle, control, decisions, pending, waiting, session_id, run_id, wait_for_resume):
    if not isinstance(request, Mapping) or request.get("kind") != "disable_ntp_and_set_time":
        raise ValueError("Invalid time approval")
    if not all(isinstance(request.get(key), str) and request[key] for key in ("id", "token", "datetime", "description")):
        raise ValueError("Incomplete time approval")
    identifier = uuid.uuid4().hex
    event = RunEvent("approval_required", {
        "approval_id": identifier,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=150)).isoformat(),
        "title": "关闭自动校时并设置时间",
        "description": request["description"],
        "actions": [{"kind": "system", "name": "disable_ntp_and_set_time",
                     "arguments": {"datetime": request["datetime"]},
                     "description": request["description"], "allowed_decisions": ["approve", "reject"]}],
    })
    waiter = asyncio.create_task(wait_for_resume(event, decisions=decisions, pending=pending, control=control,
                                                waiting=waiting, run_id=run_id, timeout_seconds=150.0))
    try:
        await asyncio.sleep(0)
        await handle.publish(event)
        action, _ = await waiter
        if control.cancelled.is_set():
            action = "cancel"
        decision = "approve" if action == "approve" else "reject"
        await handle.publish(RunEvent("approval_resolved", {"approval_id": identifier,
                                                           "decision": "timeout" if action == "timeout" else decision}))
        try:
            outcome = await _complete(request, session_id, decision)
        except Exception:
            return "时间操作结果未确认，请先检查系统时间和自动校时状态；本次不会自动重试。"
        return str(outcome["reply"]) + ("\n" + str(outcome.get("detail")) if not outcome["ok"] and outcome.get("detail") else "")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        pending.pop("id", None)
        pending.pop("resolved_id", None)
