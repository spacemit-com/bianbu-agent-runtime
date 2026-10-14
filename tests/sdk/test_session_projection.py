"""The per-Session projection file behind a zero-I/O Session listing.

Harness never folds a Session log at startup: a listing is served from the
persisted projection rows, and a cell is built on first touch.  These tests
pin the same contract for the Runtime log store — nothing is read on ``open``,
the log stays the authority the moment anything really touches a Session, and
a row that lags or lies is repaired rather than trusted.  They also pin the
two halves sharing one file: the folded row must never destroy the identity
the Session store writes beside it, and vice versa.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
import zstandard

from bianbu_agent.runs.contracts import RunResult
from bianbu_agent.sessions.log_store import SessionLogStore


def _projection_path(root: Path, session_id: str = "session-a") -> Path:
    return root / session_id / "projection.json"


def _row(root: Path, session_id: str = "session-a") -> dict:
    document = json.loads(_projection_path(root, session_id).read_text(encoding="utf-8"))
    return document["row"]


def _append_raw_event(
    path: Path,
    *,
    seq: int,
    type_: str,
    run_id: str | None = None,
    data: dict | None = None,
) -> None:
    """Append one durable frame exactly the way the store writes them."""

    document = {
        "type": "event",
        "event": {
            "seq": seq,
            "type": type_,
            "time": 100.0 + seq,
            "run_id": run_id,
            "data": data or {},
        },
    }
    frame = zstandard.ZstdCompressor(write_checksum=True).compress(
        (json.dumps(document) + "\n").encode("utf-8")
    )
    with path.open("ab") as stream:
        stream.write(frame)


async def _one_session(root: Path) -> None:
    """Materialise one Session with one finished Run, then shut down cleanly."""

    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hi"})
    await store.finish(
        "run-a",
        RunResult(request_id="run-a", status="completed", content="done"),
    )
    await store.close()


@pytest.mark.asyncio
async def test_open_reads_no_transcript(tmp_path: Path) -> None:
    """A listing must survive a transcript that cannot be read at all."""

    root = tmp_path / "sessions"
    await _one_session(root)
    transcript = root / "session-a" / "session.jsonl.zstd"
    transcript.write_bytes(b"not a transcript")

    reopened = SessionLogStore(root)
    await reopened.open()
    runs = await reopened.list()

    assert [run.request_id for run in runs] == ["run-a"]
    assert runs[0].status == "completed"
    assert reopened._indexed == set()
    with pytest.raises(ValueError, match="invalid Session transcript compression"):
        await reopened.read_session_events("session-a")
    await reopened.close()


@pytest.mark.asyncio
async def test_first_touch_folds_the_log_and_serves_it(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    await _one_session(root)

    reopened = SessionLogStore(root)
    await reopened.open()
    assert "session-a" not in reopened._indexed

    events = await reopened.read_session_events("session-a")
    assert "session-a" in reopened._indexed
    assert [(event.seq, event.type) for event in events] == [
        (0, "user/message"),
        (1, "run/completed"),
    ]
    assert (await reopened.get("run-a")).result.content == "done"
    await reopened.close()


@pytest.mark.asyncio
async def test_row_behind_the_log_is_repaired_on_touch(tmp_path: Path) -> None:
    """A crash can leave the row behind the log, never ahead of it."""

    root = tmp_path / "sessions"
    await _one_session(root)
    transcript = root / "session-a" / "session.jsonl.zstd"
    _append_raw_event(
        transcript,
        seq=2,
        type_="run/completed",
        run_id="run-a",
        data={
            "result": {
                "request_id": "run-a",
                "status": "failed",
                "content": "crashed",
                "usage": None,
                "error": None,
                "metadata": {},
            }
        },
    )

    reopened = SessionLogStore(root)
    await reopened.open()
    # The whole-device listing is the stale-but-honest rung: it shows the row's
    # answer without touching the log, so it is old rather than wrong.
    assert [run.status for run in await reopened.list()] == ["completed"]
    assert reopened._indexed == set()
    # Asking about this Session is what makes the log correct it.
    assert await reopened.session_event_tail("session-a") == 2
    assert (await reopened.list(session_id="session-a"))[0].status == "failed"
    await reopened.close()


@pytest.mark.asyncio
async def test_session_tail_asks_for_no_log_when_the_row_is_proven_current(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    await _one_session(root)

    reopened = SessionLogStore(root)
    await reopened.open()
    assert await reopened.session_event_tail("session-a") == 1
    assert reopened._indexed == set()
    await reopened.close()


@pytest.mark.asyncio
async def test_finishing_a_run_writes_the_projection_before_close(tmp_path: Path) -> None:
    """turn/end is a mandatory write point, not a throttled one."""

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hi"})
    await store.finish(
        "run-a",
        RunResult(request_id="run-a", status="completed", content="done"),
    )

    assert _projection_path(root).is_file()
    reopened = SessionLogStore(root)
    await reopened.open()
    assert [run.status for run in await reopened.list()] == ["completed"]
    await store.close()
    await reopened.close()


@pytest.mark.asyncio
async def test_a_stale_unit_sends_the_whole_row_back_to_the_log(
    tmp_path: Path,
) -> None:
    """A current byte size in one column is no evidence for another column."""

    root = tmp_path / "sessions"
    await _one_session(root)
    document = json.loads(_projection_path(root).read_text(encoding="utf-8"))
    document["row"]["units"]["inbox"]["ver"] = 99
    _projection_path(root).write_text(json.dumps(document), encoding="utf-8")

    reopened = SessionLogStore(root)
    await reopened.open()
    # A listing stays the zero-I/O rung and never looks at a unit's version...
    assert [run.status for run in await reopened.list()] == ["completed"]
    assert reopened._indexed == set()
    # ...but a row carrying a unit the live version cannot read is no proof
    # that this Session is current, so reading it goes back to the log.
    await reopened.pending_input("session-a")
    assert reopened._indexed == {"session-a"}
    await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        b"{ not json",
        b'{"row": {"ver": 99, "size": 1, "seq": 9, "units": {}}}',
        b'{"row": {"ver": 1, "size": 1, "seq": 9, "units": {'
        b'"count": {"ver": 99, "seq": 9, "val": 10}}}}',
    ],
    ids=["unparseable", "wrong-version", "wrong-unit-version"],
)
async def test_unusable_projection_is_dropped_and_the_log_still_answers(
    tmp_path: Path,
    document: bytes,
) -> None:
    root = tmp_path / "sessions"
    await _one_session(root)
    _projection_path(root).write_bytes(document)

    reopened = SessionLogStore(root)
    await reopened.open()
    assert await reopened.list() == []

    events = await reopened.read_session_events("session-a")
    assert [event.type for event in events] == ["user/message", "run/completed"]
    assert (await reopened.list(session_id="session-a"))[0].status == "completed"
    await reopened.close()


@pytest.mark.asyncio
async def test_deleting_a_session_drops_its_projection(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    await _one_session(root)

    reopened = SessionLogStore(root)
    await reopened.open()
    assert await reopened.delete_session("session-a") == 1
    assert await reopened.list() == []
    assert not (root / "session-a").exists()

    again = SessionLogStore(root)
    await again.open()
    assert await again.list() == []
    await reopened.close()
    await again.close()


@pytest.mark.asyncio
async def test_projection_flush_interval_throttles_the_row_write(tmp_path: Path) -> None:
    """Ordinary batches ride the interval; the mandatory points do not."""

    root = tmp_path / "sessions"
    store = SessionLogStore(
        root,
        write_batch_max_delay=0.01,
        projection_flush_interval=3600.0,
    )
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hi"})
    await store.flush_session("session-a")
    written = _projection_path(root).stat().st_mtime_ns

    # A row refreshed inside the interval stays in memory; the table is
    # rewritten on the next due write instead of once per event batch.
    store._pending_rows.add("session-a")
    await store._flush_projections()
    assert _projection_path(root).stat().st_mtime_ns == written

    await store._flush_projections(force=True)
    assert store._projections.dirty is False
    await store.close()


@pytest.mark.asyncio
async def test_identity_and_folded_row_share_one_file_without_clobbering(
    tmp_path: Path,
) -> None:
    """The Session record and the folded row are one file, two owners.

    A row write must preserve the identity beside it and a title rename must
    preserve the row, so neither writer can erase what the other owns.
    """

    from bianbu_agent.sessions.stores.json import JSONSessionStore

    root = tmp_path / "sessions"
    sessions = JSONSessionStore(root)
    await sessions.open()
    created = await sessions.create("session-a", agent_id="agent-a", title="first")
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hi"})
    await store.finish(
        "run-a",
        RunResult(request_id="run-a", status="completed", content="done"),
    )

    document = json.loads(_projection_path(root).read_text(encoding="utf-8"))
    assert document["record"]["title"] == "first"
    assert document["record"]["id"] == "session-a"
    assert document["row"]["size"] == (root / "session-a" / "session.jsonl.zstd").stat().st_size

    renamed = await sessions.update_title("session-a", "second")
    assert renamed.title == "second"
    assert _row(root)["size"] == document["row"]["size"]

    # The folded row is a shortcut, never the identity: dropping it leaves the
    # Session readable and only sends it back to its log.
    _projection_path(root).write_text(
        json.dumps(
            {
                "record": json.loads(
                    _projection_path(root).read_text(encoding="utf-8")
                )["record"]
            }
        ),
        encoding="utf-8",
    )
    assert (await sessions.get("session-a")).title == "second"
    reopened = SessionLogStore(root)
    await reopened.open()
    # No row means the Session is simply absent from the listing until it is
    # touched again; its log still answers, which is where the fold comes from.
    assert await reopened.list() == []
    events = await reopened.read_session_events("session-a")
    assert [event.type for event in events] == ["user/message", "run/completed"]
    assert [run.status for run in await reopened.list(session_id="session-a")] == [
        "completed"
    ]
    await store.close()
    await reopened.close()
    await sessions.close()


@pytest.mark.asyncio
async def test_a_deleted_session_directory_is_not_resurrected(tmp_path: Path) -> None:
    """A staged row must not recreate the directory of a deleted Session."""

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hi"})
    await store.flush_session("session-a")
    assert (root / "session-a" / "session.jsonl.zstd").is_file()
    store._stage_row("session-a")
    store._pending_rows.add("session-a")

    shutil.rmtree(root / "session-a")
    await store._flush_projections(force=True)

    assert not (root / "session-a").exists()
    await store.close()


@pytest.mark.asyncio
async def test_list_row_says_blank_and_when_the_session_was_prompted(
    tmp_path: Path,
) -> None:
    """Harness' ``sessionListMetadata``: blank until prompted, then the time."""

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    # A Session nobody created yet is blank and has no watermark at all.
    assert store.list_metadata("session-a") == {
        "blank": True,
        "last_prompt_at": None,
        "as_of_seq": None,
    }
    await store.start("run-a", "session-a", {"prompt": "hi"})

    live = store.list_metadata("session-a")
    assert live["blank"] is False
    assert isinstance(live["last_prompt_at"], float)
    assert live["as_of_seq"] == 0
    await store.finish(
        "run-a",
        RunResult(request_id="run-a", status="completed", content="done"),
    )
    await store.close()

    # A cold row answers the same question from the projection alone.
    reopened = SessionLogStore(root)
    await reopened.open()
    cold = reopened.list_metadata("session-a")
    assert cold["blank"] is False
    assert cold["last_prompt_at"] == live["last_prompt_at"]
    assert cold["as_of_seq"] == 1
    await reopened.close()


@pytest.mark.asyncio
async def test_console_operations_never_count_as_user_prompts(tmp_path: Path) -> None:
    """A generated title is a Run the conversation view hides, so not activity."""

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    await store.start(
        "run-title",
        "session-a",
        {"prompt": "make a title", "metadata": {"console_operation": "generate_title"}},
    )

    row = store.list_metadata("session-a")

    assert row["blank"] is True
    assert row["last_prompt_at"] is None
    await store.close()
