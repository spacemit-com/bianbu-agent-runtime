"""Session resource views and routes for the versioned HTTP API."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from functools import partial
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from bianbu_agent.api.contracts import (
    ApprovalDecisionRequest,
    CollectionEnvelope,
    PaginationQuery,
    SessionCreateRequest,
    SessionEventQuery,
    SessionSearchEnvelope,
    SessionSearchQuery,
    SessionPromptRequest,
    SessionTitleGenerateRequest,
    SessionUpdateRequest,
    SuccessEnvelope,
)
from bianbu_agent.api.runs import RunConflictError
from bianbu_agent.api.errors import success
from bianbu_agent.agents import AgentModel
from bianbu_agent.sessions.events import session_event_to_dict
from bianbu_agent.sessions.input import SessionInputConflictError
from bianbu_agent.sessions.search import search_folded
from bianbu_agent.sessions.titles import fallback_session_title


router = APIRouter(prefix="/sessions")


def session_row(runtime: object, session_id: str) -> dict[str, object]:
    """The Session's list row, read from the projection without folding.

    Harness serves a Session listing from its projection columns: the row says
    whether the Session is still blank and when it was last prompted, and a row
    nobody can vouch for leaves those facts *unknown* rather than wrong.  This
    is the same zero-I/O read, so listing never touches a transcript.
    """

    store = getattr(getattr(runtime, "runs", None), "_store", None)
    reader = getattr(store, "list_metadata", None)
    if not callable(reader):
        return dict(_EMPTY_ROW)
    try:
        row = reader(session_id)
    except (KeyError, ValueError):
        return dict(_EMPTY_ROW)
    return {**_EMPTY_ROW, **dict(row)}


def session_rows(
    runtime: object, sessions: list[object]
) -> dict[str, dict[str, object]]:
    """One list row per Session, read from the projections in one pass."""

    return {
        str(getattr(item, "id")): session_row(runtime, str(getattr(item, "id")))
        for item in sessions
    }


def session_order(
    sessions: list[object], rows: Mapping[str, Mapping[str, object]]
) -> list[object]:
    """Sessions newest activity first, with the id as the stable tie-break."""

    return sorted(
        sessions,
        key=lambda item: (
            session_activity_at(item, rows.get(str(getattr(item, "id")), _EMPTY_ROW)),
            str(getattr(item, "id")),
        ),
        reverse=True,
    )


async def session_views(runtime: object) -> list[dict[str, object]]:
    """Every Session as a list view, newest activity first.

    The REST listing and the pushed host snapshot must agree on both the order
    and the row state, so both go through here.
    """

    known = await runtime.sessions.list()
    rows = session_rows(runtime, known)
    runs = await runtime.runs.list()
    runs_by_session: dict[str, list[object]] = {}
    for run in runs:
        runs_by_session.setdefault(str(getattr(run, "session_id")), []).append(run)
    return [
        session_view(
            item,
            session_execution_view(runs_by_session.get(str(getattr(item, "id")), [])),
            runtime=runtime,
            row=rows[str(getattr(item, "id"))],
        )
        for item in session_order(known, rows)
    ]


def session_activity_at(session: object, row: Mapping[str, object]) -> float:
    """When this Session last saw activity: prompt, or the newest record write.

    Harness orders a listing by ``max(createdAt, lastPromptAt)``.  Runtime's
    identity record also carries its own ``updated_at`` (a rename, a rotated
    thread), and losing that would hide a Session the user just renamed, so the
    activity time is the newest of the three.
    """

    last_prompt = row.get("last_prompt_at")
    return max(
        float(getattr(session, "created_at", 0) or 0),
        float(getattr(session, "updated_at", 0) or 0),
        float(last_prompt or 0),
    )


def session_view(
    session: object,
    execution: Mapping[str, object] | None = None,
    *,
    runtime: object | None = None,
    row: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Serialize one Runtime Session without depending on a browser facade."""

    session_id = str(getattr(session, "id"))
    selected = session_row(runtime, session_id) if row is None else dict(row)
    last_prompt = selected.get("last_prompt_at")
    return {
        "id": session_id,
        "title": str(getattr(session, "title")),
        "agent_id": str(getattr(session, "agent_id")),
        "metadata": dict(getattr(session, "metadata", {}) or {}),
        "created_at": float(getattr(session, "created_at")),
        # Harness' ``updatedAt`` is list activity, not the record's own mtime.
        "updated_at": session_activity_at(session, selected),
        "blank": bool(selected.get("blank")),
        "last_prompt_at": None if last_prompt is None else float(last_prompt),
        "as_of_seq": selected.get("as_of_seq"),
        **dict(execution or _EMPTY_EXECUTION_VIEW),
        **session_persistence(runtime, session_id),
    }


def session_persistence(runtime: object, session_id: str) -> dict[str, object]:
    store = getattr(getattr(runtime, "runs", None), "_store", None)
    state = getattr(store, "persistence_state", None)
    return {"persistence": state(session_id)} if callable(state) else {}


_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
#: What one Session's list row says when no projection can vouch for it: never
#: prompted, never folded, no watermark.  Harness keeps such a Session visible
#: instead of hiding it, and so do we.
_EMPTY_ROW = {
    "blank": False,
    "last_prompt_at": None,
    "as_of_seq": None,
}
_EMPTY_EXECUTION_VIEW = {
    "running": False,
    "waiting": False,
    "failed": False,
    "completed_unread": False,
    "active_run_id": None,
    "pending_interaction": None,
}


async def _active_run_id(runtime: object, session_id: str) -> str:
    await runtime.sessions.get(session_id)
    runs = conversation_runs(await runtime.runs.list(session_id=session_id))
    for record in reversed(runs):
        if str(getattr(record, "status", "")) in _TERMINAL_STATUSES:
            continue
        reconciled = await runtime.runs.get_record(record.request_id)
        if (
            reconciled is not None
            and str(getattr(reconciled, "status", "")) not in _TERMINAL_STATUSES
        ):
            return str(reconciled.request_id)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "session_not_running",
            "message": "Session has no active execution",
            "details": {},
        },
    )


def session_execution_view(runs: list[object]) -> dict[str, object]:
    conversational = conversation_runs(runs)
    ordered = sorted(
        conversational,
        key=lambda item: (
            float(getattr(item, "created_at", 0) or 0),
            str(getattr(item, "request_id", "")),
        ),
    )
    active = next(
        (
            item for item in reversed(ordered)
            if str(getattr(item, "status", "")) not in _TERMINAL_STATUSES
        ),
        None,
    )
    waiting = active is not None and bool(_unresolved_approvals(active))
    latest = ordered[-1] if ordered else None
    return {
        "running": active is not None and not waiting,
        "waiting": waiting,
        "failed": latest is not None and str(getattr(latest, "status", "")) == "failed",
        "completed_unread": False,
        "active_run_id": None if active is None else str(getattr(active, "request_id")),
        "pending_interaction": "approval" if waiting else None,
    }


def _unresolved_approvals(run: object) -> set[str]:
    pending: set[str] = set()
    for event in tuple(getattr(run, "events", ())):
        identifier = str(event.payload.get("approval_id") or "")
        if event.kind == "approval_required" and identifier:
            pending.add(identifier)
        elif event.kind == "approval_resolved":
            pending.discard(identifier)
    return pending


def _event_store(runtime: object):
    store = getattr(getattr(runtime, "runs"), "_store", None)
    if store is None or not callable(getattr(store, "read_session_events", None)):
        raise RuntimeError("Session event storage is unavailable")
    return store


def conversation_runs(runs: list[object]) -> list[object]:
    """Exclude internal Runs that do not represent a user conversation turn."""

    return [
        item
        for item in runs
        if _metadata(item).get("console_operation") != "generate_title"
    ]


_ASSISTANT_MESSAGE_EVENTS = frozenset({
    "request/context",
    "hook/invoked",
    "hook/result",
    "assistant/text.delta",
    "assistant/reasoning.delta",
    "tool/call.started",
    "tool/call.progress",
    "tool/call.completed",
    "tool/call.rejected",
    "approval/requested",
    "run/completed",
    "run/failed",
    "run/cancelled",
})


def _hidden_run_ids(events: list[object]) -> set[str]:
    """Runs the conversation view hides (console operations), by Run id."""

    return {
        str(event.run_id)
        for event in events
        if event.type == "user/message"
        and event.run_id is not None
        and event.data.get("console_operation")
    }


def _history_page(
    events: list[object], max_messages: int,
) -> tuple[list[object], bool, list[str]]:
    """Return a suffix beginning at a complete visible-message boundary."""

    hidden_runs = _hidden_run_ids(events)
    starts: list[int] = []
    role_by_run: dict[str, str] = {}
    for index, event in enumerate(events):
        run_id = "" if event.run_id is None else str(event.run_id)
        if run_id in hidden_runs:
            continue
        if event.type == "user/message":
            starts.append(index)
            role_by_run[run_id] = "user"
        elif event.type == "context/compressed" and event.data.get("compressed") is not False:
            starts.append(index)
            role_by_run[run_id] = "system"
        elif event.type in _ASSISTANT_MESSAGE_EVENTS:
            if role_by_run.get(run_id) != "assistant":
                starts.append(index)
                role_by_run[run_id] = "assistant"
    if not starts:
        return events, False, sorted(hidden_runs)
    selected_message = max(0, len(starts) - max_messages)
    selected_start = starts[selected_message]
    return events[selected_start:], selected_message > 0, sorted(hidden_runs)


# Live Run streams arrive one chunk at a time.  A history window is a display
# read, so adjacent chunks of one stream fold into a single event before the
# payload is encoded: the conversation fold concatenates consecutive chunks of
# the same kind either way, while the window shrinks from "one event per token"
# to "one event per message".
_FOLDABLE_STREAM_TYPES = frozenset({
    "assistant/text.delta",
    "assistant/reasoning.delta",
})


def _fold_history_events(events: list[object]) -> list[dict[str, object]]:
    """Collapse adjacent Run-stream chunks into the first chunk of the group.

    A group keeps the ``seq`` of its first chunk so ``before_seq`` paging stays
    on message boundaries, and records the span it stands for in
    ``data.seq_range`` so a cursor reader can tell which live events it already
    covers.  Chunks are accumulated as text and rendered once per group, so a
    window costs the number of messages rather than the number of tokens.
    """

    folded: list[dict[str, object]] = []
    key: tuple[str, str | None] | None = None
    payload: dict[str, object] | None = None
    chunks: list[str] = []
    span_end: int | None = None

    def close_group() -> None:
        nonlocal key, payload, chunks, span_end
        if payload is not None:
            data = payload["data"]
            assert isinstance(data, dict)
            data["text"] = "".join(chunks)
            if span_end is not None:
                data["seq_range"] = [payload["seq"], span_end]
        key, payload, chunks, span_end = None, None, [], None

    for event in events:
        if event.type not in _FOLDABLE_STREAM_TYPES:
            close_group()
            folded.append(session_event_to_dict(event))
            continue
        chunk = "" if event.data.get("text") is None else str(event.data["text"])
        if payload is not None and key == (event.type, event.run_id):
            chunks.append(chunk)
            span_end = event.seq
            continue
        close_group()
        payload = session_event_to_dict(event)
        key, chunks = (event.type, event.run_id), [chunk]
        folded.append(payload)
    close_group()
    return folded


def _render_json(payload: object) -> bytes:
    """Render a response body with JSONResponse rules, off the event loop."""

    return json.dumps(
        jsonable_encoder(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


# A display read decodes a whole Session window out of the transcript, and an
# open Session is polled on an interval.  The transcript is append-only, so a
# rendered body stays valid until the Session tail moves; caching the bytes
# holds a few megabytes per open Session instead of the whole decoded window,
# which is what a long Session costs today.
_SESSION_READ_CACHE_ENTRIES = 16
_SESSION_READ_CACHE_BYTES = 32 * 1024 * 1024


class _SessionReadCache:
    """Rendered Session read bodies, keyed by cursor and Session tail."""

    def __init__(self, *, max_bytes: int = _SESSION_READ_CACHE_BYTES) -> None:
        self._entries: OrderedDict[tuple[object, ...], bytes] = OrderedDict()
        self._max_bytes = int(max_bytes)
        self._bytes = 0

    def get(self, key: tuple[object, ...]) -> bytes | None:
        body = self._entries.get(key)
        if body is None:
            return None
        self._entries.move_to_end(key)
        return body

    def put(self, key: tuple[object, ...], body: bytes) -> None:
        # An appended event invalidates the previous revision of the same
        # cursor, so it never lingers next to its replacement.
        prefix = key[:-1]
        for stale in [item for item in self._entries if item[:-1] == prefix]:
            self._bytes -= len(self._entries.pop(stale))
        self._entries[key] = body
        self._bytes += len(body)
        while self._entries and (
            len(self._entries) > _SESSION_READ_CACHE_ENTRIES
            or (self._bytes > self._max_bytes and len(self._entries) > 1)
        ):
            _, dropped = self._entries.popitem(last=False)
            self._bytes -= len(dropped)


def _session_read_cache(request: Request) -> _SessionReadCache:
    cache = getattr(request.app.state, "session_read_cache", None)
    if cache is None:
        cache = _SessionReadCache()
        request.app.state.session_read_cache = cache
    return cache


async def _cached_session_body(
    request: Request,
    session_id: str,
    query: SessionEventQuery,
    kind: str,
    render: Callable[[list[object], int, int | None], bytes],
    extra_key: str = "",
) -> bytes:
    """Render one Session read body, reusing the last rendering of its cursor."""

    store = _event_store(request.app.state.runtime)
    cache = _session_read_cache(request)
    tail = await store.session_event_tail(session_id)
    key: tuple[object, ...] = (
        kind, session_id, query.max_messages, query.before_seq, tail, extra_key,
    )
    body = cache.get(key)
    if body is not None:
        return body
    # A long transcript is megabytes of JSON; fold and render it off the event
    # loop so a reader never stalls the control plane.
    events = await store.read_session_events(
        session_id, before_seq=query.before_seq
    )
    tail = await store.session_event_tail(session_id)
    body = await asyncio.to_thread(
        render, list(events), query.max_messages, tail,
    )
    cache.put(
        (kind, session_id, query.max_messages, query.before_seq, tail, extra_key),
        body,
    )
    return body


async def delete_session(runtime: object, session_id: str) -> bool:
    """Delete a Session and every persisted Run it owns."""

    await runtime.sessions.get(session_id)
    deleted = await runtime.sessions.delete(session_id)
    if not deleted:
        return False
    await runtime.runs.delete_session(session_id)
    return True


@router.get("", response_model=CollectionEnvelope)
async def list_sessions(
    request: Request,
    query: Annotated[PaginationQuery, Query()],
) -> JSONResponse:
    runtime = request.app.state.runtime
    known = await runtime.sessions.list()
    rows = session_rows(runtime, known)
    sessions = session_order(known, rows)
    start = (query.page - 1) * query.page_size
    selected = sessions[start : start + query.page_size]
    runs = await runtime.runs.list()
    runs_by_session: dict[str, list[object]] = {}
    for run in runs:
        runs_by_session.setdefault(str(getattr(run, "session_id")), []).append(run)
    return _collection(
        [
            session_view(
                item,
                session_execution_view(runs_by_session.get(item.id, [])),
                runtime=runtime,
                row=rows.get(str(getattr(item, "id"))),
            )
            for item in selected
        ],
        page=query.page,
        page_size=query.page_size,
        has_more=start + len(selected) < len(sessions),
    )


def _search_session(
    events: list[object],
    needle: str,
    limit: int,
) -> list[dict[str, object]]:
    """Fold one transcript into visible messages and match them off the loop."""

    hidden_runs = _hidden_run_ids(events)
    visible = (
        [event for event in events if event.run_id is None or str(event.run_id) not in hidden_runs]
        if hidden_runs
        else events
    )
    return search_folded(_fold_history_events(visible), needle, limit=limit)


@router.get("/search", response_model=SessionSearchEnvelope)
async def search_sessions(
    request: Request,
    query: Annotated[SessionSearchQuery, Query()],
) -> Response:
    """Search conversation text across every Session, newest Session first.

    Matching is a case-insensitive substring on the folded conversation text, so
    every hit is a message the reader can open: ``seq`` is that message's anchor
    and the client pages ``/history?before_seq=seq+1`` to place it on screen.

    The scan walks each Session's memory-resident transcript; no second index is
    kept, so a hit can never disagree with ``/history``.
    """

    # ponytail: 每次查询都 O(全部会话正文)；量级变大时再加按 (session_id, seq)
    # 增量维护的缓存，届时排序与截断规则保持不变。
    runtime = request.app.state.runtime
    store = _event_store(runtime)
    needle = query.q.casefold()
    sessions = sorted(
        await runtime.sessions.list(),
        key=lambda item: (item.updated_at, item.id),
        reverse=True,
    )

    hits: list[dict[str, object]] = []
    has_more = False
    for session in sessions:
        remaining = query.limit - len(hits)
        if remaining <= 0:
            has_more = True
            break
        events = await store.read_session_events(str(session.id))
        # A long transcript is megabytes of JSON: fold and match off the event loop
        # so a search never stalls a streaming conversation.
        found = await asyncio.to_thread(_search_session, list(events), needle, remaining + 1)
        if len(found) > remaining:
            has_more = True
            found = found[:remaining]
        for hit in found:
            hits.append(
                {
                    **hit,
                    "session_id": str(session.id),
                    "title": str(session.title),
                    "updated_at": float(session.updated_at),
                }
            )
        if has_more:
            break

    return _json_response(
        _render_json(
            {
                "data": hits,
                "query": query.q,
                "limit": query.limit,
                "has_more": has_more,
            }
        )
    )


@router.post("", status_code=201, response_model=SuccessEnvelope)
async def create_session(payload: SessionCreateRequest, request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    agent = await runtime.agents.get(payload.agent_id)
    session = await runtime.sessions.create(
        session_id=payload.id,
        agent_id=payload.agent_id,
        title=payload.title,
        metadata=payload.metadata,
    )
    # Prepare only the selected expert while the user composes the first input.
    # The pool coalesces concurrent sessions and keeps creation non-blocking.
    warm = getattr(getattr(runtime, "worker_supervisor", None), "warm_agent", None)
    if callable(warm) and (
        isinstance(agent.model, AgentModel)
        or getattr(runtime, "has_default_model", False)
    ):
        warm(session.agent_id)
    return success(session_view(session, runtime=runtime), status_code=201)


@router.get("/{session_id}", response_model=SuccessEnvelope)
async def get_session(session_id: str, request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    session = await runtime.sessions.get(session_id)
    runs = await runtime.runs.list(session_id=session_id)
    return success(session_view(session, session_execution_view(runs), runtime=runtime))


@router.patch("/{session_id}", response_model=SuccessEnvelope)
async def update_session(
    session_id: str,
    payload: SessionUpdateRequest,
    request: Request,
) -> JSONResponse:
    runtime = request.app.state.runtime
    sessions = runtime.sessions
    if "title" not in payload.model_fields_set:
        session = await sessions.get(session_id)
    elif payload.title is None:
        raise ValueError("session title must not be null")
    else:
        session = await sessions.rename(session_id, payload.title)
    runs = await runtime.runs.list(session_id=session_id)
    return success(session_view(session, session_execution_view(runs), runtime=runtime))


@router.delete("/{session_id}", status_code=204)
async def remove_session(session_id: str, request: Request) -> Response:
    await request.app.state.run_broker.delete_session(session_id)
    return Response(status_code=204)


@router.post("/{session_id}/clear-context", response_model=SuccessEnvelope)
async def clear_context(session_id: str, request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    session = await runtime.sessions.clear_context(session_id)
    runs = await runtime.runs.list(session_id=session_id)
    return success(session_view(session, session_execution_view(runs), runtime=runtime))


@router.post(
    "/{session_id}/prompt",
    status_code=202,
    response_model=SuccessEnvelope,
)
async def prompt_session(
    session_id: str,
    payload: SessionPromptRequest,
    request: Request,
) -> JSONResponse:
    runtime = request.app.state.runtime
    session = await runtime.sessions.get(session_id)
    agent = await runtime.agents.get(session.agent_id)
    if not isinstance(agent.model, AgentModel) and not getattr(runtime, "has_default_model", False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "agent_model_required",
                "message": "当前专家尚未配置模型，请先为该专家选择服务商和模型。",
                "details": {"agent_id": agent.id},
            },
        )
    try:
        result = await request.app.state.session_input_broker.prompt(
            session_id,
            message_id=payload.message_id,
            content=payload.content,
            attachments=tuple(payload.attachments),
            skill_ids=tuple(payload.skill_ids),
        )
    except SessionInputConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_input_conflict",
                "message": str(exc),
                "details": {},
            },
        ) from exc
    return success(result, status_code=202)


@router.post(
    "/{session_id}/generate-title",
    status_code=202,
    response_model=SuccessEnvelope,
)
async def generate_session_title(
    session_id: str,
    payload: SessionTitleGenerateRequest,
    request: Request,
) -> JSONResponse:
    runtime = request.app.state.runtime
    session = await runtime.sessions.get(session_id)
    title = session.title or fallback_session_title(payload.content)
    if not session.title:
        session = await runtime.sessions.rename(session_id, title)
    digest = hashlib.sha256(payload.message_id.encode("utf-8")).hexdigest()
    snapshot = await request.app.state.run_broker.submit(
        session_id=session_id,
        prompt=payload.content,
        idempotency_key=f"title:{digest}",
        agent_id=session.agent_id,
        metadata={"console_operation": "generate_title"},
    )
    request.app.state.session_title_manager.track(
        session_id,
        str(snapshot["id"]),
        title,
    )
    return success(
        {
            "title": title,
            "run_id": str(snapshot["id"]),
            "status": str(snapshot["status"]),
        },
        status_code=202,
    )


@router.post("/{session_id}/cancel", response_model=SuccessEnvelope)
async def cancel_session(session_id: str, request: Request) -> JSONResponse:
    try:
        run_id = await _active_run_id(request.app.state.runtime, session_id)
    except HTTPException:
        raise
    try:
        await request.app.state.run_broker.cancel(run_id)
    except (KeyError, RunConflictError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_control_conflict",
                "message": str(exc).strip("'\"") or "Session control was not accepted",
                "details": {},
            },
        ) from exc
    return success({"accepted": True})


@router.post(
    "/{session_id}/approvals/{approval_id}",
    response_model=SuccessEnvelope,
)
async def resolve_session_approval(
    session_id: str,
    approval_id: str,
    payload: ApprovalDecisionRequest,
    request: Request,
) -> JSONResponse:
    run_id = await _active_run_id(request.app.state.runtime, session_id)
    try:
        await request.app.state.run_broker.resolve_approval(
            run_id,
            approval_id=approval_id,
            decision=payload.decision.value,
            reason=payload.reason,
        )
    except (KeyError, RunConflictError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_control_conflict",
                "message": str(exc).strip("'\"") or "Session control was not accepted",
                "details": {},
            },
        ) from exc
    return success({"accepted": True})


@router.get("/{session_id}/events")
async def list_session_events(
    session_id: str,
    request: Request,
    query: Annotated[SessionEventQuery, Query()],
) -> Response:
    runtime = request.app.state.runtime
    await runtime.sessions.get(session_id)
    store = _event_store(runtime)
    if query.from_seq is not None:
        # A recovery reader already holds a contiguous cursor, so it needs the
        # unbroken stream from that position rather than the newest window.  The
        # page stays unfolded: folding would break `seq`, and the reader treats a
        # page that does not start exactly where it asked as a protocol error.
        return _json_response(
            _render_json(await _cursor_body(store, session_id, query.from_seq))
        )
    # The persistence state rides along with the window, so it has to be part of
    # the cache key: a write failure or a recovery changes it without moving the
    # event tail.
    persistence = session_persistence(runtime, session_id)
    body = await _cached_session_body(
        request,
        session_id,
        query,
        "events",
        partial(_events_body, persistence=persistence),
        extra_key=json.dumps(persistence, sort_keys=True, default=str),
    )
    return _json_response(body)


#: Upper bound on one incremental recovery page.  A gap can be arbitrarily wide,
#: so the response is capped and the reader keeps asking while ``has_more``.
_CURSOR_PAGE_EVENTS = 512

#: The count cap alone still lets a page of large events grow past the size a
#: reader accepts, so one page also carries a byte budget.  The first event is
#: always kept, so a reader that holds a cursor still advances on a huge event.
_CURSOR_PAGE_BYTES = 4 * 1024 * 1024


async def _cursor_body(
    store: object,
    session_id: str,
    from_seq: int,
) -> dict[str, object]:
    events = await store.read_session_events(
        session_id,
        from_seq=from_seq,
        before_seq=from_seq + _CURSOR_PAGE_EVENTS,
    )
    tail = await store.session_event_tail(session_id)
    data: list[dict[str, object]] = []
    first = from_seq
    last = from_seq - 1
    budget = _CURSOR_PAGE_BYTES
    for event in events:
        item = session_event_to_dict(event)
        cost = len(_render_json(item)) + 1  # +1: the comma before the next item
        if data and cost > budget:
            # Cut the tail rather than the head: the page stays contiguous and
            # starts where the reader asked, so it can keep asking for the rest.
            break
        budget -= cost
        if not data:
            first = event.seq
        last = event.seq
        data.append(item)
    return {
        "data": data,
        "base_seq": first,
        "last_seq": tail,
        "has_more": tail is not None and last < tail,
    }


def _events_body(
    events: list[object],
    max_messages: int,
    tail: int | None,
    persistence: Mapping[str, object] | None = None,
) -> bytes:
    selected, has_more, hidden_run_ids = _history_page(events, max_messages)
    return _render_json({
        "data": [session_event_to_dict(event) for event in selected],
        "base_seq": selected[0].seq if selected else 0,
        "last_seq": tail,
        "has_more": has_more,
        "hidden_run_ids": hidden_run_ids,
        **dict(persistence or {}),
    })


@router.get("/{session_id}/history")
async def read_session_history(
    session_id: str,
    request: Request,
    query: Annotated[SessionEventQuery, Query()],
) -> Response:
    """Read the display window of one Session as folded conversation events.

    The raw ``/events`` window is the recovery contract: a cursor reader needs
    an unbroken ``seq`` stream.  A display reader instead wants the last
    ``max_messages`` messages, so this view folds each Run stream into one
    event and the payload tracks messages rather than streamed chunks.
    ``from_seq`` belongs to that raw view and is rejected here.
    """

    runtime = request.app.state.runtime
    if query.from_seq is not None:
        # Folding rewrites ``seq``, so this view cannot answer a cursor read at
        # all: a recovery reader asserts that the page starts where it asked.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "session_history_read_mode_unsupported",
                "message": "from_seq is only supported on the raw events view",
                "details": {"view": "history"},
            },
        )
    await runtime.sessions.get(session_id)
    body = await _cached_session_body(
        request, session_id, query, "history", _history_body,
    )
    return _json_response(body)


def _history_body(
    events: list[object],
    max_messages: int,
    tail: int | None,
) -> bytes:
    selected, has_more, hidden_run_ids = _history_page(events, max_messages)
    folded = _fold_history_events(selected)
    return _render_json({
        "data": folded,
        "base_seq": folded[0]["seq"] if folded else 0,
        "last_seq": tail,
        "has_more": has_more,
        "hidden_run_ids": hidden_run_ids,
    })


def _json_response(body: bytes) -> Response:
    return Response(content=body, media_type="application/json")


def _collection(
    data: list[dict[str, object]],
    *,
    page: int,
    page_size: int,
    has_more: bool,
    extra: Mapping[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        jsonable_encoder(
            {
                "data": data,
                "page": {"number": page, "size": page_size, "has_more": has_more},
                **dict(extra or {}),
            }
        )
    )


def _metadata(run: object) -> dict[str, object]:
    request = getattr(run, "request", {})
    if not isinstance(request, Mapping):
        return {}
    metadata = request.get("metadata")
    return dict(metadata) if isinstance(metadata, Mapping) else {}


__all__ = [
    "conversation_runs",
    "delete_session",
    "router",
    "session_view",
]
