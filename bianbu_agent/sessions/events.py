"""Durable Session event envelopes and the canonical conversation fold."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from bianbu_agent.runs import RunEvent, RunResult, encode_result
from bianbu_agent.sessions.attachments import enrich_attachments
from bianbu_agent.sessions.inbox import (
    SessionInboxMessage,
    inbox_message_to_dict,
)


_RUN_EVENT_TYPES = {
    "progress": "run/progress",
    "request_context": "request/context",
    "hook_invoked": "hook/invoked",
    "hook_result": "hook/result",
    "reasoning": "assistant/reasoning.delta",
    "token": "assistant/text.delta",
    "message_end": "assistant/message.end",
    "tool_start": "tool/call.started",
    "tool_progress": "tool/call.progress",
    "tool_end": "tool/call.completed",
    "tool_rejected": "tool/call.rejected",
    "approval_required": "approval/requested",
    "approval_resolved": "approval/resolved",
    "context_compression": "context/compressed",
    "input_applied": "agent/input.applied",
}

#: Every event type this build can write into a Session transcript.  A reader
#: treats one of these as understood; any other ``type`` must carry
#: ``ignorable`` or the transcript is rejected rather than quietly losing an
#: event the reader does not know how to rebuild.
SESSION_EVENT_TYPES = frozenset(
    {
        "user/message",
        "run/event",
        "run/completed",
        "run/failed",
        "run/cancelled",
        "agent/inbox/claimed",
        "agent/inbox/spliced",
        *_RUN_EVENT_TYPES.values(),
    }
)


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One immutable event in a Session-local contiguous log."""

    session_id: str
    seq: int
    type: str
    time: float
    run_id: str | None
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        session_id = str(self.session_id).strip()
        event_type = str(self.type).strip()
        if not session_id:
            raise ValueError("session_id must not be empty")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ValueError("seq must be a non-negative integer")
        if not event_type:
            raise ValueError("type must not be empty")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "type", event_type)
        object.__setattr__(self, "time", float(self.time))
        object.__setattr__(self, "run_id", None if self.run_id is None else str(self.run_id))
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


def session_event_for_run_start(
    session_id: str, run_id: str, request: Mapping[str, object], *,
    seq: int, event_time: float,
) -> SessionEvent:
    metadata = request.get("metadata")
    selected_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    attachments = selected_metadata.get("attachments")
    data: dict[str, object] = {
        "content": str(request.get("prompt") or ""),
        "attachments": list(attachments) if isinstance(attachments, (list, tuple)) else [],
        "thread_id": str(request.get("thread_id") or session_id),
        # Harness keeps request facts in the Session log.  This detached copy
        # is log-only metadata; the conversation reducer ignores it while Run
        # snapshots can be reconstructed without a second persistent record.
        "request": dict(request),
    }
    if selected_metadata.get("console_operation"):
        data["console_operation"] = str(selected_metadata["console_operation"])
    return SessionEvent(
        session_id, seq, "user/message", event_time, run_id,
        data,
    )


def session_event_for_run_event(
    session_id: str, run_id: str, event: RunEvent, *,
    seq: int, event_time: float,
) -> SessionEvent:
    event_type = _RUN_EVENT_TYPES.get(event.kind)
    if event_type is None:
        return SessionEvent(
            session_id, seq, "run/event", event_time, run_id,
            {"kind": event.kind, "payload": dict(event.payload)},
        )
    return SessionEvent(session_id, seq, event_type, event_time, run_id, event.payload)


def session_event_for_run_result(
    session_id: str, result: RunResult, *, seq: int, event_time: float,
) -> SessionEvent:
    event_type = {
        "completed": "run/completed",
        "cancelled": "run/cancelled",
    }.get(str(result.status), "run/failed")
    encoded = encode_result(result)
    return SessionEvent(
        session_id, seq, event_type, event_time, result.request_id,
        {
            "status": str(result.status),
            "content": result.content,
            "usage": encoded["usage"],
            "error": encoded["error"],
            "result": encoded,
        },
    )


def session_event_for_inbox_splice(
    session_id: str,
    message: SessionInboxMessage,
    *,
    seq: int,
    event_time: float,
) -> SessionEvent:
    return SessionEvent(
        session_id,
        seq,
        "agent/inbox/spliced",
        event_time,
        None,
        {"message": inbox_message_to_dict(message)},
    )


def session_event_for_inbox_claim(
    session_id: str,
    run_id: str,
    message_ids: tuple[str, ...],
    *,
    seq: int,
    event_time: float,
) -> SessionEvent:
    return SessionEvent(
        session_id,
        seq,
        "agent/inbox/claimed",
        event_time,
        run_id,
        {"message_ids": list(message_ids)},
    )


def session_event_for_inbox_user_message(
    session_id: str,
    run_id: str,
    message: SessionInboxMessage,
    *,
    seq: int,
    event_time: float,
    request: Mapping[str, object] | None = None,
) -> SessionEvent:
    data: dict[str, object] = {
        "message_id": message.message_id,
        "content": message.content,
        "attachments": enrich_attachments(session_id, message.attachments),
        "thread_id": str((request or {}).get("thread_id") or session_id),
    }
    if request is not None:
        data["request"] = dict(request)
    return SessionEvent(
        session_id,
        seq,
        "user/message",
        event_time,
        run_id,
        data,
    )


def session_event_to_dict(event: SessionEvent) -> dict[str, object]:
    data = dict(event.data)
    attachments = data.get("attachments")
    if isinstance(attachments, (list, tuple)):
        # A Session written before the preview URL was derived on the server
        # still has its bytes on disk, so a replayed message gets the preview
        # back here instead of rendering an attachment it cannot show.
        data["attachments"] = enrich_attachments(event.session_id, attachments)
    return {
        "seq": event.seq,
        "type": event.type,
        "time": event.time,
        "run_id": event.run_id,
        "data": data,
    }


__all__ = [
    "SESSION_EVENT_TYPES",
    "SessionEvent", "session_event_for_run_event",
    "session_event_for_inbox_claim", "session_event_for_inbox_splice",
    "session_event_for_inbox_user_message",
    "session_event_for_run_result", "session_event_for_run_start",
    "session_event_to_dict",
]
