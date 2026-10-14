"""The three physical transcript row shapes.

Harness writes one append-only JSONL transcript per Session whose rows come in
three shapes: one header row, one row per event, and packed ``*-chunks`` rows
that stand for a whole run of stream deltas.  These tests pin the third shape,
the ``ignorable`` escape hatch for rows a reader does not understand, and the
rule that a packed row must rebuild its events exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import zstandard

from bianbu_agent.runs.events import RunEvent
from bianbu_agent.sessions import log_store as module
from bianbu_agent.sessions.events import SessionEvent
from bianbu_agent.sessions.log_store import SessionLogStore, read_session_transcript


def _rows(path: Path) -> list[dict[str, object]]:
    """Decode every frame of one transcript into its rows."""

    remaining = path.read_bytes()
    rows: list[dict[str, object]] = []
    while remaining:
        decoder = zstandard.ZstdDecompressor().decompressobj()
        payload = decoder.decompress(remaining)
        assert decoder.eof
        remaining = decoder.unused_data
        rows.extend(
            json.loads(line) for line in payload.decode("utf-8").split("\n") if line
        )
    return rows


def _append_frame(path: Path, *documents: dict[str, object]) -> None:
    """Append rows exactly the way one store append batch would."""

    frame = zstandard.ZstdCompressor(write_checksum=True).compress(
        ("\n".join(json.dumps(row) for row in documents) + "\n").encode("utf-8")
    )
    with path.open("ab") as stream:
        stream.write(frame)


def _frames(path: Path) -> list[bytes]:
    """Split one transcript into its raw, still-compressed zstd frames."""

    remaining = path.read_bytes()
    frames: list[bytes] = []
    while remaining:
        decoder = zstandard.ZstdDecompressor().decompressobj()
        decoder.decompress(remaining)
        assert decoder.eof
        consumed = len(remaining) - len(decoder.unused_data)
        frames.append(remaining[:consumed])
        remaining = decoder.unused_data
    return frames


def _header(session_id: str = "session-a") -> dict[str, object]:
    return {"type": "session", "version": 1, "id": session_id, "created_at": 1.0}


def _delta(index: int, *, type_: str = "assistant/text.delta", run_id: str = "run-a",
           text: str | None = None, extra: dict[str, object] | None = None) -> SessionEvent:
    return SessionEvent(
        "session-a",
        index,
        type_,
        1.0 + index * 0.25,
        run_id,
        {"text": f"t{index}" if text is None else text, **(extra or {})},
    )


def test_pack_a_whole_stream_run_into_one_row() -> None:
    batch = tuple(_delta(index) for index in range(1, 6))

    lines = module._transcript_documents(batch)

    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["type"] == "text-chunks"
    assert row["seq0"] == 1
    assert row["time0"] == pytest.approx(1.25)
    assert row["run_id"] == "run-a"
    assert row["data"]["texts"] == ["t1", "t2", "t3", "t4", "t5"]
    # 契约是 len(texts) == len(dt) + 1：首段从 time0 起，每个 dt 是到下一段的一步。
    assert row["data"]["dt"] == [250, 250, 250, 250]
    assert len(row["data"]["texts"]) == len(row["data"]["dt"]) + 1


def test_short_runs_and_other_events_stay_one_row_per_event() -> None:
    batch = (
        _delta(0, text="solo"),
        _delta(1, text="pair"),
        SessionEvent("session-a", 2, "tool/call.completed", 2.0, "run-a", {"result": "{}"}),
        _delta(3, text="tail"),
    )

    rows = [json.loads(line) for line in module._transcript_documents(batch)]

    assert [row["type"] for row in rows] == ["event", "event", "event", "event"]
    assert [row["event"]["seq"] for row in rows] == [0, 1, 2, 3]


def test_a_run_is_only_packed_while_type_run_and_metadata_stay_uniform() -> None:
    batch = (
        _delta(0),
        _delta(1),
        _delta(2),
        # 同一 Run 但换了事件类型：新开一组。
        _delta(3, type_="assistant/reasoning.delta"),
        _delta(4, type_="assistant/reasoning.delta"),
        _delta(5, type_="assistant/reasoning.delta"),
        # 同一类型但换了 Run：又新开一组。
        _delta(6, run_id="run-b"),
        _delta(7, run_id="run-b"),
        _delta(8, run_id="run-b"),
        # 元数据变了（多一个 index）：不再跟上一组同列。
        _delta(9, extra={"index": 9}),
        _delta(10, extra={"index": 9}),
        _delta(11, extra={"index": 9}),
    )

    rows = [json.loads(line) for line in module._transcript_documents(batch)]

    assert [row["type"] for row in rows] == [
        "text-chunks", "reasoning-chunks", "text-chunks", "text-chunks",
    ]
    assert [row["seq0"] for row in rows] == [0, 3, 6, 9]
    assert all(row["data"]["dt"] == [250, 250] for row in rows)
    assert rows[3]["data"]["index"] == 9


@pytest.mark.asyncio
async def test_a_packed_transcript_reads_back_the_events_it_was_written_from(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "question"})
    for index in range(12):
        await store.append("run-a", RunEvent("token", {"text": f"part-{index}"}))
    live = await store.read_session_events("session-a")
    await store.flush_session("session-a")
    await store.close()

    transcript = root / "session-a" / "session.jsonl.zstd"
    rows = _rows(transcript)
    assert [row["type"] for row in rows] == ["session", "event", "text-chunks"]
    assert len(rows) < len(live)

    cold = read_session_transcript(root, "session-a")
    assert [event.seq for event in cold] == list(range(len(live)))
    assert [event.type for event in cold] == [event.type for event in live]
    assert [dict(event.data) for event in cold] == [dict(event.data) for event in live]
    assert [event.time for event in cold] == pytest.approx(
        [event.time for event in live], abs=1e-3
    )

    reopened = SessionLogStore(root)
    await reopened.open()
    rebuilt = await reopened.read_session_events("session-a")
    await reopened.close()
    assert [dict(event.data) for event in rebuilt] == [
        dict(event.data) for event in live
    ]


@pytest.mark.asyncio
async def test_the_header_owns_the_first_frame_alone(tmp_path: Path) -> None:
    """Harness decodes frame one knowing it is exactly one header line.

    A reader that only wants the metadata must never have to decode an event
    batch, so the header may not share its frame with the first append.
    """

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "question"})
    await store.append("run-a", RunEvent("token", {"text": "part"}))
    await store.flush_session("session-a")
    await store.close()

    frames = _frames(root / "session-a" / "session.jsonl.zstd")
    assert len(frames) >= 2
    first = zstandard.ZstdDecompressor().decompress(frames[0])
    assert first.count(b"\n") == 1
    assert first.endswith(b"\n")
    header = json.loads(first)
    assert header["type"] == "session"
    assert header["version"] == 1
    assert header["id"] == "session-a"
    # 首帧之后不再出现 header 行，header 全文件只落一次。
    rest = zstandard.ZstdDecompressor().decompress(b"".join(frames[1:]))
    assert b'"type":"session"' not in rest


def test_unknown_event_type_is_rejected_without_the_ignorable_mark(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    transcript = root / "session-a" / "session.jsonl.zstd"
    transcript.parent.mkdir(parents=True)
    _append_frame(transcript, _header())
    _append_frame(transcript, {
        "type": "event",
        "event": {
            "seq": 0, "type": "future/thing", "time": 2.0, "run_id": None, "data": {},
        },
    })

    with pytest.raises(ValueError, match="without ignorable"):
        read_session_transcript(root, "session-a")


def test_an_ignorable_row_is_skipped_and_leaves_the_sequence_contiguous(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    transcript = root / "session-a" / "session.jsonl.zstd"
    transcript.parent.mkdir(parents=True)
    _append_frame(transcript, _header())
    _append_frame(transcript, {
        "type": "event",
        "event": {
            "seq": 0, "type": "future/thing", "time": 2.0, "run_id": None, "data": {},
        },
        "ignorable": True,
    })
    _append_frame(transcript, {
        "type": "event",
        "event": {
            "seq": 1, "type": "user/message", "time": 3.0, "run_id": "run-a",
            "data": {"content": "hello"},
        },
    })

    events = read_session_transcript(root, "session-a")

    assert [event.seq for event in events] == [1]
    assert events[0].data["content"] == "hello"


def test_an_unknown_packed_row_is_rejected_rather_than_dropped(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    transcript = root / "session-a" / "session.jsonl.zstd"
    transcript.parent.mkdir(parents=True)
    _append_frame(transcript, _header())
    # ``tool-call-chunks`` 是本仓刻意不写的形态，读端必须拒绝而不是静默丢掉
    # 三个它看不懂的事件。
    _append_frame(transcript, {
        "type": "tool-call-chunks",
        "seq0": 0,
        "time0": 2.0,
        "data": {"texts": ["a", "b", "c"], "dt": [1, 1], "name": "terminal"},
    })

    with pytest.raises(ValueError):
        read_session_transcript(root, "session-a")


def test_a_packed_row_must_carry_one_more_text_than_step(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    transcript = root / "session-a" / "session.jsonl.zstd"
    transcript.parent.mkdir(parents=True)
    _append_frame(transcript, _header())
    _append_frame(transcript, {
        "type": "text-chunks",
        "seq0": 0,
        "time0": 2.0,
        "data": {"texts": ["a", "b", "c"], "dt": [1, 1, 1]},
    })

    with pytest.raises(ValueError, match="invalid packed"):
        read_session_transcript(root, "session-a")
