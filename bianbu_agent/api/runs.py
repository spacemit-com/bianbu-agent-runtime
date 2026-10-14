"""Internal execution services used by Session-owned HTTP resources."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from bianbu_agent.runs.events import RunEvent
from bianbu_agent.runs import RunHandle, RunResult
from bianbu_agent.server.runtime import BianbuAgentRuntime
from bianbu_agent.sessions import SessionAgentMismatchError


_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class RunConflictError(RuntimeError):
    """A Run command cannot be accepted in the current lifecycle state."""


class IdempotencyConflictError(RunConflictError):
    """An idempotency key was replayed with a different request body."""


class RunBroker:
    """Project persisted Runtime Runs into repeatable HTTP snapshots and events."""

    def __init__(self, runtime: BianbuAgentRuntime) -> None:
        self._runtime = runtime
        self._submit_lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._pending_reservations: set[tuple[str, str, str]] = set()

    async def submit(
        self,
        *,
        session_id: str,
        prompt: str,
        idempotency_key: str,
        agent_id: str | None = None,
        attachments: list[dict[str, object]] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_session = str(session_id).strip()
        normalized_prompt = str(prompt).strip()
        normalized_key = str(idempotency_key).strip()
        if not normalized_session:
            raise ValueError("session_id must not be empty")
        if not normalized_prompt:
            raise ValueError("prompt must not be empty")
        if not normalized_key:
            raise ValueError("idempotency_key must not be empty")

        async with self._submit_lock:
            title_only = (
                dict(metadata or {}).get("console_operation") == "generate_title"
            )
            if title_only:
                session = await self._runtime.sessions.get(normalized_session)
                supplied_agent = str(agent_id or "").strip()
                if supplied_agent and supplied_agent != session.agent_id:
                    raise SessionAgentMismatchError(
                        normalized_session,
                        session.agent_id,
                        supplied_agent,
                    )
            else:
                session = await self._runtime.sessions.get_or_create(
                    session_id=normalized_session,
                    agent_id=agent_id,
                )
            fingerprint = _request_fingerprint(
                session_id=normalized_session,
                prompt=normalized_prompt,
                agent_id=session.agent_id,
                attachments=attachments,
                metadata=metadata,
            )
            session_runs = await self._runtime.runs.list(
                session_id=normalized_session
            )
            reconciled_runs = []
            for item in session_runs:
                reconciled = (
                    await self._runtime.runs.get_record(item.request_id)
                    if item.status not in _TERMINAL_STATUSES
                    else item
                )
                if reconciled is not None:
                    reconciled_runs.append(reconciled)
            session_runs = reconciled_runs
            existing = next(
                (
                    item
                    for item in session_runs
                    if str(
                        dict(getattr(item, "request", {}) or {}).get(
                            "idempotency_key"
                        )
                        or ""
                    )
                    == normalized_key
                ),
                None,
            )
            if existing is not None:
                stored = dict(getattr(existing, "request", {}) or {})
                stored_fingerprint = str(stored.get("idempotency_fingerprint") or "")
                if not stored_fingerprint:
                    stored_metadata = dict(stored.get("metadata") or {})
                    if "attachments" in stored_metadata:
                        raise IdempotencyConflictError(
                            "legacy Run request cannot be safely matched to this idempotency key"
                        )
                    stored_attachments = stored_metadata.pop("attachments", None)
                    stored_fingerprint = _request_fingerprint(
                        session_id=str(existing.session_id),
                        prompt=str(stored.get("prompt") or ""),
                        agent_id=str(stored.get("agent_id") or session.agent_id),
                        attachments=stored_attachments,
                        metadata=stored_metadata,
                    )
                if stored_fingerprint != fingerprint:
                    raise IdempotencyConflictError(
                        "idempotency key is already bound to a different Run request"
                    )
                return await self.snapshot(str(existing.request_id))
            active = next(
                (
                    item
                    for item in session_runs
                    if item.status not in _TERMINAL_STATUSES
                    and _is_title_record(item) == title_only
                ),
                None,
            )
            if active is not None:
                raise RunConflictError(
                    "a Run is already active for this Session"
                )
            run_metadata = dict(metadata or {})
            if attachments:
                run_metadata["attachments"] = list(attachments)
            handle = await session.run(
                normalized_prompt,
                metadata=run_metadata,
                idempotency_key=normalized_key,
                idempotency_fingerprint=fingerprint,
            )
        return await self.snapshot(handle.request_id)

    async def delete_session(self, session_id: str) -> bool:
        """Serialize Session deletion with Run admission for the same store."""

        from bianbu_agent.api.sessions import delete_session

        async with self._submit_lock:
            return await delete_session(self._runtime, session_id)

    async def snapshot(self, run_id: str) -> dict[str, object]:
        record = await self._record_and_reconcile(run_id)
        pending = _pending_actions(record.events)
        result = record.result
        status = str(record.status)
        if status not in _TERMINAL_STATUSES and pending:
            status = "waiting"
        request = dict(record.request)
        content = (
            str(result.content)
            if isinstance(result, RunResult)
            else "".join(
                str(event.payload.get("text") or "")
                for event in record.events
                if event.kind == "token"
            )
        )
        return {
            "id": record.request_id,
            "session_id": record.session_id,
            "agent_id": str(request.get("agent_id") or ""),
            "status": status,
            "content": content,
            "usage": _usage(result),
            "error": _error(result),
            "pending_actions": [] if status in _TERMINAL_STATUSES else pending,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    async def cancel(self, run_id: str) -> bool:
        return await self._control(run_id, lambda handle: handle.cancel())

    async def resolve_approval(
        self,
        run_id: str,
        *,
        approval_id: str,
        decision: str,
        reason: str = "",
    ) -> bool:
        async def route(handle: RunHandle) -> bool:
            if decision == "approve":
                return await handle.approve(approval_id)
            if decision == "reject":
                return await handle.reject(approval_id, reason=reason)
            raise ValueError("decision must be approve or reject")

        return await self._pending_control(
            run_id,
            action_type="approval",
            action_id=approval_id,
            route=route,
        )

    async def _record(self, run_id: str):
        selected = str(run_id)
        record = await self._runtime.runs.get_record(selected)
        if record is None:
            raise KeyError(selected)
        return record

    async def _active_handle(self, run_id: str) -> RunHandle:
        record = await self._record(run_id)
        if record.status in _TERMINAL_STATUSES:
            raise RunConflictError("Run is not active")
        handle = await self._runtime.runs.get(str(run_id))
        if handle is None:
            raise RunConflictError("Run is not active")
        return handle

    async def _control(
        self,
        run_id: str,
        route: Callable[[RunHandle], Awaitable[bool]],
    ) -> bool:
        accepted = await route(await self._active_handle(run_id))
        if not accepted:
            raise RunConflictError("Run control was not accepted")
        return True

    async def _pending_control(
        self,
        run_id: str,
        *,
        action_type: str,
        action_id: str,
        route: Callable[[RunHandle], Awaitable[bool]],
    ) -> bool:
        handle = await self._active_handle(run_id)
        reservation = await self._reserve_pending_action(
            run_id,
            action_type=action_type,
            action_id=action_id,
        )
        accepted = False
        try:
            for _ in range(20):
                route_task = asyncio.ensure_future(route(handle))
                try:
                    accepted = bool(await asyncio.shield(route_task))
                except asyncio.CancelledError as cancelled:
                    route_task.cancel()
                    try:
                        accepted = bool(await route_task)
                    except (asyncio.CancelledError, Exception):
                        accepted = False
                    raise cancelled
                if accepted:
                    return True
                record = await self._record(run_id)
                if record.status in _TERMINAL_STATUSES:
                    break
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            if not accepted:
                await self._release_pending_reservation(reservation)
            raise
        except Exception:
            await self._release_pending_reservation(reservation)
            raise
        await self._release_pending_reservation(reservation)
        raise RunConflictError("Run control was not accepted")

    async def _reserve_pending_action(
        self,
        run_id: str,
        *,
        action_type: str,
        action_id: str,
    ) -> tuple[str, str, str]:
        key = (str(run_id), action_type, str(action_id))
        identifier_name = f"{action_type}_id"
        required_type = f"{action_type}_required"
        async with self._pending_lock:
            record = await self._record(run_id)
            self._reconcile_pending_reservations_locked(record)
            if key in self._pending_reservations:
                raise RunConflictError("Pending Run action is already reserved")
            pending = _pending_actions(record.events)
            if not any(
                item.get("type") == required_type
                and str(item.get(identifier_name) or "") == key[2]
                for item in pending
            ):
                raise RunConflictError("Pending Run action was not found")
            self._pending_reservations.add(key)
        return key

    async def _release_pending_reservation(
        self,
        key: tuple[str, str, str],
    ) -> None:
        async with self._pending_lock:
            self._pending_reservations.discard(key)

    async def _record_and_reconcile(self, run_id: str):
        async with self._pending_lock:
            record = await self._record(run_id)
            self._reconcile_pending_reservations_locked(record)
            return record

    def _reconcile_pending_reservations_locked(self, record) -> None:
        run_id = str(record.request_id)
        if record.status in _TERMINAL_STATUSES:
            self._pending_reservations.difference_update(
                {key for key in self._pending_reservations if key[0] == run_id}
            )
            return
        pending = _pending_actions(record.events)
        unresolved = {
            (
                run_id,
                str(item["type"]).removesuffix("_required"),
                str(item.get("approval_id") or ""),
            )
            for item in pending
        }
        self._pending_reservations.difference_update(
            {
                key
                for key in self._pending_reservations
                if key[0] == run_id and key not in unresolved
            }
        )


def _request_fingerprint(
    *, session_id: str, prompt: str, agent_id: str,
    attachments: object, metadata: object,
) -> str:
    canonical = json.dumps(
        {
            "session_id": str(session_id).strip(),
            "prompt": str(prompt).strip(),
            "agent_id": str(agent_id).strip(),
            "attachments": attachments or [],
            "metadata": metadata or {},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_title_record(record: object) -> bool:
    request = getattr(record, "request", {})
    if not isinstance(request, dict):
        return False
    metadata = request.get("metadata")
    return (
        isinstance(metadata, dict)
        and metadata.get("console_operation") == "generate_title"
    )


def _pending_actions(events: tuple[RunEvent, ...]) -> list[dict[str, object]]:
    pending: dict[tuple[str, str], dict[str, object]] = {}
    for event in events:
        if event.kind == "approval_required":
            action = {"type": event.kind, **dict(event.payload)}
            identifier_name = "approval_id"
            identifier = str(action.get(identifier_name) or "")
            if identifier:
                pending[(event.kind.removesuffix("_required"), identifier)] = action
        elif event.kind == "approval_resolved":
            identifier_name = "approval_id"
            identifier = str(event.payload.get(identifier_name) or "")
            pending.pop((event.kind.removesuffix("_resolved"), identifier), None)
    return list(pending.values())


def _usage(result: RunResult | None) -> dict[str, Any] | None:
    if result is None or result.usage is None:
        return None
    return {
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "total_tokens": result.usage.total_tokens,
        "metadata": dict(result.usage.metadata),
    }


def _error(result: RunResult | None) -> dict[str, Any] | None:
    if result is None or result.error is None:
        return None
    return {
        "code": result.error.code,
        "message": result.error.message,
        "details": dict(result.error.details),
    }


__all__ = ["RunBroker"]
