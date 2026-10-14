from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest
import zstandard

import bianbu_agent.sessions as sessions
from bianbu_agent.sessions import SessionInboxMessage, SessionLogStore
from bianbu_agent.sessions.inbox import inbox_message_from_dict, inbox_message_to_dict
from bianbu_agent.runs import RunEvent


def test_session_package_exposes_immutable_inbox_values() -> None:
    """Removing Session ownership or making accepted input mutable is a bug."""

    message_type = getattr(sessions, "SessionInboxMessage", None)
    snapshot_type = getattr(sessions, "SessionInboxSnapshot", None)

    assert message_type is not None
    assert snapshot_type is not None

    message = message_type(
        message_id="message-a",
        content="change direction",
        attachments=({"name": "context.txt"},),
        created_at=10.5,
    )
    snapshot = snapshot_type(next_step=(message,))

    assert message.message_id == "message-a"
    assert message.attachments == ({"name": "context.txt"},)
    assert snapshot.next_step == (message,)
    with pytest.raises(FrozenInstanceError):
        message.content = "mutated"


def test_inbox_skill_ids_are_normalized_and_survive_round_trip() -> None:
    """Dropping or duplicating a selected Skill during replay is a bug."""

    message = SessionInboxMessage(
        "message-skill",
        "review this",
        skill_ids=(" global/review ", "global/review", "global/security"),
    )

    assert message.skill_ids == ("global/review", "global/security")
    assert inbox_message_from_dict(inbox_message_to_dict(message)) == message


def test_inbox_rejects_empty_selected_skill_id() -> None:
    """An empty capability ID must not become an ambiguous runtime override."""

    with pytest.raises(ValueError, match="skill_ids must not contain empty values"):
        SessionInboxMessage("message-skill", "review this", skill_ids=("",))


def _message(message_id: str, content: str) -> SessionInboxMessage:
    return SessionInboxMessage(
        message_id=message_id,
        content=content,
        created_at=float(len(message_id)),
    )


@pytest.mark.asyncio
async def test_inbox_replays_and_claims_all_next_step_messages(
    tmp_path,
) -> None:
    """All steering present at one boundary is claimed as one FIFO batch."""

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    for message in (
        _message("step-1", "first steer"),
        _message("step-2", "second steer"),
    ):
        assert await store.admit_input("session-a", message) is True

    claimed = await store.claim_input("session-a", run_id="run-a")

    assert [message.message_id for message in claimed] == ["step-1", "step-2"]
    assert await store.pending_input("session-a") == sessions.SessionInboxSnapshot()
    await store.close()

    reopened = SessionLogStore(root)
    await reopened.open()
    snapshot = await reopened.pending_input("session-a")
    assert snapshot.next_step == ()
    events = await reopened.read_session_events("session-a")
    assert [event.type for event in events] == [
        "agent/inbox/spliced",
        "agent/inbox/spliced",
        "agent/inbox/claimed",
    ]
    assert events[-1].run_id == "run-a"
    assert events[-1].data["message_ids"] == ["step-1", "step-2"]
    await reopened.close()


@pytest.mark.asyncio
async def test_inbox_admission_is_idempotent_but_rejects_conflicting_replay(
    tmp_path,
) -> None:
    """A transport retry must not duplicate or silently replace accepted input."""

    store = SessionLogStore(tmp_path / "sessions")
    await store.open()
    original = _message("message-a", "use plan A")

    assert await store.admit_input("session-a", original) is True
    assert await store.admit_input("session-a", original) is False
    with pytest.raises(ValueError, match="message_id.*different input"):
        await store.admit_input(
            "session-a", _message("message-a", "use plan B")
        )
    with pytest.raises(ValueError, match="message_id.*different input"):
        await store.admit_input(
            "session-a",
            SessionInboxMessage(
                "message-a",
                "use plan A",
                skill_ids=("global.skills/review",),
            ),
        )

    snapshot = await store.pending_input("session-a")
    assert snapshot.next_step == (original,)
    await store.close()


@pytest.mark.asyncio
async def test_failed_durable_admission_never_changes_pending_projection(
    tmp_path, monkeypatch
) -> None:
    """Reporting a write failure while retaining a phantom input is a bug."""

    store = SessionLogStore(tmp_path / "sessions")
    await store.open()

    async def fail_persist(*_args, **_kwargs) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "_persist_batch", fail_persist)
    with pytest.raises(OSError, match="disk unavailable"):
        await store.admit_input(
            "session-a", _message("message-a", "later")
        )
    assert await store.pending_input("session-a") == sessions.SessionInboxSnapshot()
    await store.close()


@pytest.mark.asyncio
async def test_first_touch_rejects_a_duplicate_persisted_inbox_splice(tmp_path) -> None:
    """Silently replaying a corrupt duplicate splice would break idempotency.

    ``open`` no longer reads any transcript — the Runtime folds a Session when
    something actually asks for it — so the refusal belongs to that first
    touch, which is exactly the call that would have replayed the duplicate.
    """

    root = tmp_path / "sessions"
    store = SessionLogStore(root)
    await store.open()
    message = _message("message-a", "later")
    await store.admit_input("session-a", message)
    await store.close()

    transcript = root / "session-a" / "session.jsonl.zstd"
    duplicate = {
        "type": "event",
        "event": {
            "seq": 1,
            "type": "agent/inbox/spliced",
            "time": 11.0,
            "run_id": None,
            "data": {
                "message": {
                    "message_id": "message-a",
                    "content": "later",
                    "attachments": [],
                    "created_at": 9.0,
                }
            },
        },
    }
    transcript.write_bytes(
        transcript.read_bytes()
        + zstandard.ZstdCompressor(write_checksum=True).compress(
            (json.dumps(duplicate) + "\n").encode()
        )
    )

    reopened = SessionLogStore(root)
    await reopened.open()
    with pytest.raises(ValueError, match="duplicate persisted inbox message_id"):
        await reopened.pending_input("session-a")


@pytest.mark.asyncio
async def test_applied_step_input_claims_pending_and_becomes_user_history(
    tmp_path,
) -> None:
    """A receipt-only steer must stay pending; an applied one must hand off once."""

    root = tmp_path / "sessions"
    store = SessionLogStore(root)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "original"})
    message = SessionInboxMessage(
        "message-step",
        "change direction",
        attachments=({"name": "context.txt"},),
    )
    await store.admit_input("session-a", message)

    before = await store.read_session_events("session-a")
    assert [event.data.get("message_id") for event in before] == [None, None]

    await store.append(
        "run-a",
        RunEvent("input_applied", {"message_id": "message-step"}),
    )

    assert await store.pending_input("session-a") == sessions.SessionInboxSnapshot()
    visible = [
        event
        for event in await store.read_session_events("session-a")
        if event.type == "user/message"
    ]
    assert [(event.data.get("message_id"), event.data["content"]) for event in visible] == [
        (None, "original"),
        ("message-step", "change direction"),
    ]
    await store.close()

    reopened = SessionLogStore(root)
    await reopened.open()
    assert await reopened.pending_input("session-a") == sessions.SessionInboxSnapshot()
    assert await reopened.input_run_id("session-a", "message-step") == "run-a"
    await reopened.close()
