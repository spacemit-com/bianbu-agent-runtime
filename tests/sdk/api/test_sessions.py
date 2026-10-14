from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bianbu_agent.api import sessions as api_sessions
from bianbu_agent.runs import RunError, RunEvent, RunResult, Usage
from bianbu_agent.sessions.titles import SessionTitleError
from tests.sdk.api.test_runs import _api, _graph_api


async def _wait_for_run(runtime, run_id: str, *, status: str = "completed"):
    for _ in range(100):
        record = await runtime.runs.get_record(run_id)
        if record is not None and record.status == status:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run did not reach {status}: {run_id}")


async def _completed_run(
    client, runtime, session_id: str, prompt: str, *, metadata: dict[str, object] | None = None
) -> str:
    """Create one real terminal Run for a history-focused API test."""

    await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
    response = await client.post(
        f"/api/v1/sessions/{session_id}/prompt",
        json={
            "message_id": f"history-{len(prompt)}-{sum(map(ord, prompt))}",
            "content": prompt,
        },
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    await _wait_for_run(runtime, run_id)
    return run_id


@pytest.mark.asyncio
async def test_generate_title_immediately_persists_fallback(tmp_path: Path) -> None:
    """Title model latency cannot leave a newly created Session untitled."""

    async with _api(tmp_path) as (client, runtime):
        created = await client.post(
            "/api/v1/sessions",
            json={"id": "title-owner", "agent_id": "default"},
        )
        assert created.status_code == 201

        response = await client.post(
            "/api/v1/sessions/title-owner/generate-title",
            json={
                "message_id": "message-title-owner",
                "content": "请帮我检查 K3 服务状态",
            },
        )

        assert response.status_code == 202
        assert response.json()["data"]["title"] == "请帮我检查 K3 服务状态"
        session = await client.get("/api/v1/sessions/title-owner")
        assert session.json()["data"]["title"] == "请帮我检查 K3 服务状态"


@pytest.mark.asyncio
async def test_prompt_model_less_agent_guides_user_to_configure_model(tmp_path: Path) -> None:
    """Prompt admission fails before queuing work when the expert has no model."""

    async with _api(tmp_path) as (client, runtime):
        runtime._models._default = None
        agent = await client.post("/api/v1/agents", json={"name": "Draft"})
        agent_id = agent.json()["data"]["id"]
        warmed: list[str] = []
        had_pool = hasattr(runtime, "worker_supervisor")
        original_pool = getattr(runtime, "worker_supervisor", None)
        runtime.worker_supervisor = type(
            "RecordingPool",
            (),
            {"warm_agent": lambda _self, selected: warmed.append(selected)},
        )()
        try:
            created = await client.post(
                "/api/v1/sessions",
                json={"id": "draft-session", "agent_id": agent_id},
            )
            response = await client.post(
                "/api/v1/sessions/draft-session/prompt",
                json={"message_id": "draft-message", "content": "你好"},
            )
        finally:
            if had_pool:
                runtime.worker_supervisor = original_pool
            else:
                del runtime.worker_supervisor

    assert created.status_code == 201
    assert warmed == []
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "agent_model_required",
        "message": "当前专家尚未配置模型，请先为该专家选择服务商和模型。",
        "details": {"agent_id": agent_id},
    }


@pytest.mark.asyncio
async def test_generate_title_publishes_late_auxiliary_result(tmp_path: Path) -> None:
    """A successful detached title request replaces its owned fallback."""

    async with _graph_api(tmp_path) as (client, _runtime, _model):
        await client.post(
            "/api/v1/sessions",
            json={"id": "late-title", "agent_id": "default"},
        )

        response = await client.post(
            "/api/v1/sessions/late-title/generate-title",
            json={
                "message_id": "message-late-title",
                "content": "inspect K3",
            },
        )

        assert response.status_code == 202
        assert response.json()["data"]["title"] == "inspect K3"
        for _ in range(100):
            current = await client.get("/api/v1/sessions/late-title")
            if current.json()["data"]["title"] == "followup saw direct history":
                break
            await asyncio.sleep(0.01)
        assert current.json()["data"]["title"] == "followup saw direct history"


@pytest.mark.asyncio
async def test_title_failure_keeps_fallback_title_and_completes_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reasoning-only title answers must not fail the auxiliary Run.

    MiniMax spends the whole title budget on in-band reasoning, so the visible
    answer stays empty.  The Run keeps the deterministic fallback title the
    Console already shows instead of reporting a failed Run.
    """

    async def reasoning_only_title(*_args, **_kwargs) -> str:
        raise SessionTitleError("session title model produced no text")

    monkeypatch.setattr(
        "bianbu_agent.runs.manager.generate_session_title",
        reasoning_only_title,
    )
    async with _graph_api(tmp_path) as (client, runtime, _model):
        await client.post(
            "/api/v1/sessions",
            json={"id": "fallback-title", "agent_id": "default"},
        )

        response = await client.post(
            "/api/v1/sessions/fallback-title/generate-title",
            json={"message_id": "message-fallback", "content": "inspect K3"},
        )

        assert response.status_code == 202
        assert response.json()["data"]["title"] == "inspect K3"
        await _wait_for_run(runtime, response.json()["data"]["run_id"])
        current = await client.get("/api/v1/sessions/fallback-title")
        assert current.json()["data"]["title"] == "inspect K3"


@pytest.mark.asyncio
async def test_pending_title_never_blocks_next_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session admission ignores detached title work still using the model."""

    title_started = asyncio.Event()
    release_title = asyncio.Event()

    async def slow_title(*_args, **_kwargs) -> str:
        title_started.set()
        await release_title.wait()
        return "late title"

    monkeypatch.setattr(
        "bianbu_agent.runs.manager.generate_session_title",
        slow_title,
    )
    async with _api(tmp_path) as (client, _runtime):
        await client.post(
            "/api/v1/sessions",
            json={"id": "nonblocking-title", "agent_id": "default"},
        )
        title = await client.post(
            "/api/v1/sessions/nonblocking-title/generate-title",
            json={
                "message_id": "message-nonblocking-title",
                "content": "first message",
            },
        )
        assert title.status_code == 202
        await asyncio.wait_for(title_started.wait(), timeout=1)

        try:
            prompt = await client.post(
                "/api/v1/sessions/nonblocking-title/prompt",
                json={
                    "message_id": "next-message",
                    "content": "hi",
                },
            )

            assert prompt.status_code == 202
            assert prompt.json()["data"]["status"] == "claimed"
            assert prompt.json()["data"]["run_id"]
        finally:
            release_title.set()


@pytest.mark.asyncio
async def test_late_title_does_not_overwrite_explicit_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asynchronous provider only owns the fallback revision it created."""

    title_started = asyncio.Event()
    release_title = asyncio.Event()

    async def slow_title(*_args, **_kwargs) -> str:
        title_started.set()
        await release_title.wait()
        return "stale generated title"

    monkeypatch.setattr(
        "bianbu_agent.runs.manager.generate_session_title",
        slow_title,
    )
    async with _api(tmp_path) as (client, runtime):
        await client.post(
            "/api/v1/sessions",
            json={"id": "renamed-title", "agent_id": "default"},
        )
        response = await client.post(
            "/api/v1/sessions/renamed-title/generate-title",
            json={"message_id": "first-message", "content": "first fallback"},
        )
        assert response.status_code == 202
        await asyncio.wait_for(title_started.wait(), timeout=1)

        renamed = await client.patch(
            "/api/v1/sessions/renamed-title",
            json={"title": "my explicit title"},
        )
        assert renamed.status_code == 200
        release_title.set()
        await _wait_for_run(
            runtime,
            response.json()["data"]["run_id"],
            status="completed",
        )
        await asyncio.sleep(0.02)

        current = await client.get("/api/v1/sessions/renamed-title")
        assert current.json()["data"]["title"] == "my explicit title"


@pytest.mark.asyncio
async def test_session_events_persist_per_run_token_usage(
    tmp_path: Path,
) -> None:
    """History usage follows conversational Runs, not pages or internal Runs."""

    async with _api(tmp_path) as (client, runtime):
        session_id = "token-usage"
        await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
        first_id = "usage-first"
        second_id = "usage-second"
        store = runtime.runs._store
        await store.start(first_id, session_id, {"prompt": "first"})
        await store.finish(
            first_id,
            RunResult(
                request_id=first_id,
                status="completed",
                content="first",
                usage=Usage(input_tokens=10, output_tokens=3, total_tokens=13),
            ),
        )
        await store.start(second_id, session_id, {"prompt": "second"})
        await store.finish(
            second_id,
            RunResult(
                request_id=second_id,
                status="completed",
                content="second",
                usage=Usage(input_tokens=20, output_tokens=5, total_tokens=25),
            ),
        )
        await _completed_run(client, runtime, session_id, "without usage")
        title_id = "usage-title"
        await store.start(
            title_id, session_id,
            {
                "prompt": "internal title",
                "metadata": {"console_operation": "generate_title"},
            },
        )
        await store.finish(
            title_id,
            RunResult(
                request_id=title_id,
                status="completed",
                content="title",
                usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
            ),
        )

        response = await client.get(f"/api/v1/sessions/{session_id}/events")

        assert response.status_code == 200
        assert response.json()["hidden_run_ids"] == [title_id]
        terminal_by_run = {
            item["run_id"]: item["data"]
            for item in response.json()["data"]
            if item["type"] == "run/completed"
        }
        assert terminal_by_run[first_id]["usage"] == {
            "input_tokens": 10,
            "output_tokens": 3,
            "total_tokens": 13,
            "metadata": {},
        }
        assert terminal_by_run[second_id]["usage"] == {
            "input_tokens": 20,
            "output_tokens": 5,
            "total_tokens": 25,
            "metadata": {},
        }
        assert terminal_by_run[title_id]["usage"]["total_tokens"] == 150


@pytest.mark.asyncio
async def test_session_history_projects_reasoning_for_refresh(tmp_path: Path) -> None:
    """A persisted reasoning stream must survive Session history hydration."""

    async with _api(tmp_path) as (client, runtime):
        run_id = await _completed_run(client, runtime, "reasoning-history", "answer")
        await runtime.runs._store.append(
            run_id,
            RunEvent("reasoning", {"text": "saved "}),
        )
        await runtime.runs._store.append(
            run_id,
            RunEvent("reasoning", {"text": "plan"}),
        )
        await runtime.runs._store.flush_session("reasoning-history")

        response = await client.get("/api/v1/sessions/reasoning-history/events")

        assert response.status_code == 200
        reasoning = [
            item["data"]["text"]
            for item in response.json()["data"]
            if item["type"] == "assistant/reasoning.delta"
        ]
        assert reasoning == ["saved ", "plan"]


@pytest.mark.asyncio
async def test_session_events_stay_readable_when_the_writer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写盘失败不该让用户看不到对话：读内存，保存状态单独上报。"""

    from bianbu_agent.sessions import log_store as log_store_module

    monkeypatch.setattr(log_store_module, "_DURABLE_STALL_SECONDS", 0)
    async with _api(tmp_path) as (client, runtime):
        run_id = await _completed_run(client, runtime, "failing-writes", "answer")
        store = runtime.runs._store

        async def failing(session_id, batch):
            raise OSError("No space left on device")

        store._persist_batch = failing  # type: ignore[method-assign]
        await store.append(run_id, RunEvent("token", {"text": "uncommitted"}))
        with pytest.raises(OSError):
            await store.flush_session("failing-writes")

        response = await client.get("/api/v1/sessions/failing-writes/events")

        assert response.status_code == 200
        payload = response.json()
        assert payload["data"], "写盘失败时对话必须仍然可读"
        assert "uncommitted" in str(payload), "内存里已有的内容不能被写盘失败挡住"
        persistence = payload["persistence"]
        assert persistence["state"] == "error"
        assert persistence["error"]["code"] == "session_log_write_failed"
        assert "No space" in persistence["error"]["details"]["cause"]
        assert persistence["pending_events"] >= 1


@pytest.mark.asyncio
async def test_history_reads_do_not_wait_for_pending_disk_writes(tmp_path: Path) -> None:
    async with _api(tmp_path) as (client, runtime):
        run_id = await _completed_run(client, runtime, "stalled-writes", "answer")
        store = runtime.runs._store
        original = store._persist_batch
        release = asyncio.Event()

        async def stalled(session_id, batch):
            await release.wait()
            await original(session_id, batch)

        store._persist_batch = stalled
        await store.append(run_id, RunEvent("token", {"text": "pending"}))
        try:
            response = await asyncio.wait_for(
                client.get("/api/v1/sessions/stalled-writes/events"), timeout=.2,
            )
            assert response.status_code == 200
            assert response.json()["data"]
        finally:
            release.set()
            await store.flush_session("stalled-writes")


@pytest.mark.asyncio
async def test_session_events_are_contiguous_and_pageable(tmp_path: Path) -> None:
    async with _api(tmp_path) as (client, runtime):
        first_id = await _completed_run(client, runtime, "raw-history", "first")
        second_id = await _completed_run(client, runtime, "raw-history", "second")

        response = await client.get(
            "/api/v1/sessions/raw-history/events?max_messages=2"
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["data"][0]["run_id"] == second_id
        assert payload["data"][0]["type"] == "user/message"
        assert payload["base_seq"] == payload["data"][0]["seq"]
        assert payload["last_seq"] == payload["data"][-1]["seq"]
        assert payload["has_more"] is True

        older = await client.get(
            f"/api/v1/sessions/raw-history/events?before_seq={payload['base_seq']}"
        )
        older_payload = older.json()
        assert older_payload["data"][0]["run_id"] == first_id
        assert older_payload["data"][0]["type"] == "user/message"
        assert older_payload["has_more"] is False

        obsolete = await client.get(
            "/api/v1/sessions/raw-history/events?max_events=1"
        )
        assert obsolete.status_code == 422


@pytest.mark.asyncio
async def test_session_events_cursor_read_returns_the_unbroken_stream(
    tmp_path: Path,
) -> None:
    """A recovery reader that holds a cursor asks for what it missed.

    The window read answers "show me the conversation" and may fold a Run into
    one message; the cursor read must stay unfolded, because the reader checks
    that the page starts exactly where it asked and treats a folded page as a
    protocol error.
    """

    async with _api(tmp_path) as (client, runtime):
        await _completed_run(client, runtime, "cursor-read", "first")
        await _completed_run(client, runtime, "cursor-read", "second")

        window = await client.get(
            "/api/v1/sessions/cursor-read/events?max_messages=50"
        )
        window_payload = window.json()

        response = await client.get(
            "/api/v1/sessions/cursor-read/events?from_seq=0"
        )

        assert response.status_code == 200
        payload = response.json()
        seqs = [item["seq"] for item in payload["data"]]
        assert payload["base_seq"] == 0
        assert seqs == list(range(len(seqs)))
        assert payload["last_seq"] == seqs[-1]
        assert payload["has_more"] is False
        assert len(seqs) >= len(window_payload["data"])

        partial = await client.get(
            f"/api/v1/sessions/cursor-read/events?from_seq={payload['base_seq'] + 1}"
        )
        partial_payload = partial.json()
        assert partial_payload["data"][0]["seq"] == 1


@pytest.mark.asyncio
async def test_session_events_cursor_read_pages_while_has_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wide gap is read in capped pages instead of one unbounded response."""

    from bianbu_agent.api import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_CURSOR_PAGE_EVENTS", 2)
    async with _api(tmp_path) as (client, runtime):
        await _completed_run(client, runtime, "cursor-pages", "first")
        await _completed_run(client, runtime, "cursor-pages", "second")

        seen: list[int] = []
        cursor = 0
        for _ in range(200):
            response = await client.get(
                f"/api/v1/sessions/cursor-pages/events?from_seq={cursor}"
            )
            assert response.status_code == 200
            payload = response.json()
            assert len(payload["data"]) <= 2
            seqs = [item["seq"] for item in payload["data"]]
            assert seqs[0] == cursor
            seen.extend(seqs)
            if not payload["has_more"]:
                break
            cursor = seqs[-1] + 1
        else:
            raise AssertionError("cursor paging did not terminate")

        assert seen == list(range(len(seen)))


@pytest.mark.asyncio
async def test_session_events_cursor_read_cuts_the_page_by_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page of large events is bounded by size, not only by event count."""

    from bianbu_agent.api import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_CURSOR_PAGE_BYTES", 1)
    async with _api(tmp_path) as (client, runtime):
        await _completed_run(client, runtime, "cursor-bytes", "only")

        seen: list[int] = []
        cursor = 0
        for _ in range(200):
            response = await client.get(
                f"/api/v1/sessions/cursor-bytes/events?from_seq={cursor}"
            )
            assert response.status_code == 200
            payload = response.json()
            # The first event is always kept, so a reader still advances.
            assert len(payload["data"]) == 1
            seqs = [item["seq"] for item in payload["data"]]
            assert seqs[0] == cursor
            seen.extend(seqs)
            if not payload["has_more"]:
                break
            cursor = seqs[-1] + 1
        else:
            raise AssertionError("byte-bounded paging did not terminate")

        assert seen == list(range(len(seen)))


@pytest.mark.asyncio
async def test_session_events_cursor_read_past_the_tail_is_empty(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, runtime):
        await _completed_run(client, runtime, "cursor-past", "only")

        response = await client.get(
            "/api/v1/sessions/cursor-past/events?from_seq=9999"
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["data"] == []
        assert payload["has_more"] is False


@pytest.mark.asyncio
async def test_session_history_rejects_the_cursor_read_mode(
    tmp_path: Path,
) -> None:
    """The folded display view cannot serve a cursor reader's seq invariant."""

    async with _api(tmp_path) as (client, runtime):
        await _completed_run(client, runtime, "cursor-history", "only")

        response = await client.get(
            "/api/v1/sessions/cursor-history/history?from_seq=0"
        )

        assert response.status_code == 422
        assert (
            response.json()["error"]["code"]
            == "session_history_read_mode_unsupported"
        )


@pytest.mark.asyncio
async def test_session_events_read_modes_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, runtime):
        await _completed_run(client, runtime, "cursor-mixed", "only")

        response = await client.get(
            "/api/v1/sessions/cursor-mixed/events?from_seq=0&before_seq=3"
        )

        assert response.status_code == 422


@pytest.mark.asyncio
async def test_session_tail_is_fifty_complete_messages_not_event_chunks(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, runtime):
        session_id = "message-window"
        await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
        store = runtime.runs._store
        for index in range(26):
            run_id = f"window-{index}"
            await store.start(run_id, session_id, {"prompt": f"question {index}"})
            for chunk in range(8):
                await store.append(
                    run_id,
                    RunEvent("reasoning", {"text": f"thought {index}.{chunk} "}),
                )
            await store.append(run_id, RunEvent("token", {"text": f"answer {index}"}))
            await store.finish(run_id, RunResult(run_id, "completed", f"answer {index}"))

        response = await client.get(
            f"/api/v1/sessions/{session_id}/events?max_messages=50"
        )
        payload = response.json()
        assert response.status_code == 200
        assert payload["has_more"] is True
        assert payload["data"][0]["run_id"] == "window-1"
        assert payload["data"][0]["type"] == "user/message"
        assert len([
            item for item in payload["data"]
            if item["run_id"] == "window-1"
            and item["type"] == "assistant/reasoning.delta"
        ]) == 8

        older = await client.get(
            f"/api/v1/sessions/{session_id}/events"
            f"?before_seq={payload['base_seq']}&max_messages=50"
        )
        assert {item["run_id"] for item in older.json()["data"]} == {"window-0"}


def _fold_stream_chunks(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Client-side fold of the raw window, mirroring the browser reducer."""

    folded: list[dict[str, object]] = []
    for event in events:
        previous = folded[-1] if folded else None
        if (
            previous is not None
            and event["type"] in {"assistant/text.delta", "assistant/reasoning.delta"}
            and event["type"] == previous["type"]
            and event["run_id"] == previous["run_id"]
        ):
            previous["data"]["text"] = (
                f'{previous["data"]["text"]}{event["data"]["text"]}'
            )
            span = previous["data"].setdefault(
                "seq_range", [previous["seq"], previous["seq"]],
            )
            span[1] = event["seq"]
            continue
        data = dict(event["data"])
        if event["type"] in {"assistant/text.delta", "assistant/reasoning.delta"}:
            data["text"] = str(data.get("text") or "")
        folded.append({**event, "data": data})
    return folded


@pytest.mark.asyncio
async def test_session_history_window_folds_each_run_stream(
    tmp_path: Path,
) -> None:
    """A display window must ship one event per stream, not one per token."""

    async with _api(tmp_path) as (client, runtime):
        session_id = "folded-window"
        await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
        store = runtime.runs._store
        await store.start("fold-run", session_id, {"prompt": "question"})
        for chunk in range(2000):
            await store.append("fold-run", RunEvent("reasoning", {"text": f"{chunk},"}))
        await store.append("fold-run", RunEvent("token", {"text": "answer"}))
        for chunk in range(500):
            await store.append("fold-run", RunEvent("reasoning", {"text": "-"}))
        await store.finish(
            "fold-run", RunResult("fold-run", "completed", "answer"),
        )

        raw = (
            await client.get(f"/api/v1/sessions/{session_id}/events?max_messages=50")
        ).json()
        history = (
            await client.get(f"/api/v1/sessions/{session_id}/history?max_messages=50")
        ).json()

        assert len(raw["data"]) == 2503
        folded = history["data"]
        assert len(folded) < 10
        # 折叠不改变重建结果：客户端折叠原始窗口得到同一份载荷。
        assert _fold_stream_chunks(raw["data"]) == folded
        reasoning = [item for item in folded if item["type"] == "assistant/reasoning.delta"]
        assert [item["run_id"] for item in reasoning] == ["fold-run", "fold-run"]
        assert reasoning[0]["data"]["text"] == "".join(f"{chunk}," for chunk in range(2000))
        assert reasoning[0]["data"]["seq_range"] == [1, 2000]
        # 中间插入的 token 事件隔断了相邻性，因此不是被并进同一组。
        assert reasoning[1]["data"]["text"] == "-" * 500
        assert reasoning[1]["data"]["seq_range"] == [2002, 2501]
        # 折叠组以首条 seq 定位，翻页游标因此仍落在消息边界上。
        assert reasoning[0]["seq"] == 1
        assert history["base_seq"] == folded[0]["seq"]
        assert history["last_seq"] == raw["last_seq"]
        assert history["has_more"] is False
        assert [item["seq"] for item in folded] == sorted(
            {item["seq"] for item in folded}
        )


@pytest.mark.asyncio
async def test_session_history_window_pages_back_by_folded_cursor(
    tmp_path: Path,
) -> None:
    """The folded window keeps ``before_seq`` on the same message boundaries."""

    async with _api(tmp_path) as (client, runtime):
        await _completed_run(client, runtime, "folded-paging", "first")
        await _completed_run(client, runtime, "folded-paging", "second")

        newest = (
            await client.get("/api/v1/sessions/folded-paging/history?max_messages=2")
        ).json()
        older = (
            await client.get(
                "/api/v1/sessions/folded-paging/history"
                f"?before_seq={newest['base_seq']}&max_messages=2"
            )
        ).json()

        assert newest["has_more"] is True
        assert older["has_more"] is False
        assert older["base_seq"] < newest["base_seq"]
        assert {item["seq"] for item in older["data"]}.isdisjoint(
            {item["seq"] for item in newest["data"]}
        )
        raw_older = (
            await client.get(
                "/api/v1/sessions/folded-paging/events"
                f"?before_seq={newest['base_seq']}&max_messages=2"
            )
        ).json()
        # 事件窗口仍是恢复契约：窗口内 seq 连续，折叠只发生在显示读模型里。
        raw_seqs = [item["seq"] for item in raw_older["data"]]
        assert raw_seqs == list(range(raw_seqs[0], raw_seqs[0] + len(raw_seqs)))
        # 同一窗口的两个读模型互为折叠/展开，翻页游标因此可以互换。
        assert _fold_stream_chunks(raw_older["data"]) == [
            {**item, "data": {**item["data"]}} for item in older["data"]
        ]


@pytest.mark.asyncio
async def test_session_list_projects_running_waiting_and_failed_states(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, runtime):
        await runtime.sessions.get_or_create(session_id="running-session", agent_id="default")
        running = await runtime.runs._store.start(
            "running-run", "running-session", {"prompt": "run"}
        )
        assert running is None

        await runtime.sessions.get_or_create(session_id="waiting-session", agent_id="default")
        await runtime.runs._store.start(
            "waiting-run", "waiting-session", {"prompt": "wait"}
        )
        await runtime.runs._store.append(
            "waiting-run",
            RunEvent("approval_required", {"approval_id": "approval-1", "actions": []}),
        )

        await runtime.sessions.get_or_create(session_id="failed-session", agent_id="default")
        await runtime.runs._store.start(
            "failed-run", "failed-session", {"prompt": "fail"}
        )
        await runtime.runs._store.finish(
            "failed-run",
            RunResult(
                "failed-run", "failed",
                error=RunError("boom", "failed"),
            ),
        )

        await runtime.sessions.get_or_create(session_id="title-only-session", agent_id="default")
        await runtime.runs._store.start(
            "title-run", "title-only-session",
            {
                "prompt": "make a title",
                "metadata": {"console_operation": "generate_title"},
            },
        )

        response = await client.get("/api/v1/sessions?page=1&page_size=20")
        by_id = {item["id"]: item for item in response.json()["data"]}

        assert {
            key: by_id["running-session"][key]
            for key in (
                "running", "waiting", "failed", "completed_unread",
                "active_run_id", "pending_interaction",
            )
        } == {
            "running": True,
            "waiting": False,
            "failed": False,
            "completed_unread": False,
            "active_run_id": "running-run",
            "pending_interaction": None,
        }
        assert by_id["waiting-session"]["waiting"] is True
        assert by_id["waiting-session"]["running"] is False
        assert by_id["waiting-session"]["pending_interaction"] == "approval"
        assert by_id["failed-session"]["failed"] is True
        assert by_id["failed-session"]["active_run_id"] is None
        assert by_id["title-only-session"]["running"] is False
        assert by_id["title-only-session"]["active_run_id"] is None

        await runtime.sessions.rename("title-only-session", "Published title")
        renamed = await client.get("/api/v1/sessions?page=1&page_size=20")
        renamed_by_id = {item["id"]: item for item in renamed.json()["data"]}
        assert renamed_by_id["title-only-session"]["title"] == "Published title"


@pytest.mark.asyncio
async def test_session_list_row_is_blank_until_prompted_and_orders_by_activity(
    tmp_path: Path,
) -> None:
    """Harness' list row: blank, lastPromptAt, and ``updatedAt`` as activity.

    A Session that only exists is blank; being prompted clears that and moves
    it to the front of the listing even though a later Session record exists.
    """

    async with _api(tmp_path) as (client, runtime):
        older = await runtime.sessions.get_or_create(session_id="older", agent_id="default")
        newer = await runtime.sessions.get_or_create(session_id="newer", agent_id="default")
        assert newer.created_at >= older.created_at

        unprompted = await client.get("/api/v1/sessions?page=1&page_size=20")
        blank_by_id = {item["id"]: item for item in unprompted.json()["data"]}
        assert blank_by_id["older"]["blank"] is True
        assert blank_by_id["older"]["last_prompt_at"] is None
        assert blank_by_id["older"]["as_of_seq"] is None

        await runtime.runs._store.start("run-older", "older", {"prompt": "hello"})
        await runtime.runs._store.finish(
            "run-older", RunResult("run-older", "completed", "hi")
        )

        prompted = await client.get("/api/v1/sessions?page=1&page_size=20")
        items = prompted.json()["data"]
        by_id = {item["id"]: item for item in items}
        assert by_id["older"]["blank"] is False
        assert by_id["older"]["last_prompt_at"] > newer.created_at
        assert by_id["older"]["updated_at"] == by_id["older"]["last_prompt_at"]
        assert by_id["newer"]["blank"] is True
        assert [item["id"] for item in items] == ["older", "newer"]


@pytest.mark.asyncio
async def test_sessions_are_bound_paginated_and_title_patchable(tmp_path: Path) -> None:
    """A Session resource retains its Agent binding and supports bounded pages."""

    async with _api(tmp_path) as (client, runtime):
        first = await client.post(
            "/api/v1/sessions",
            json={"agent_id": "writer", "title": "Research", "metadata": {"tag": "api"}},
        )
        second = await client.post(
            "/api/v1/sessions", json={"agent_id": "default", "title": "Later"}
        )

        assert first.status_code == 201
        assert second.status_code == 201
        created = first.json()["data"]
        assert created["agent_id"] == "writer"
        assert created["title"] == "Research"
        assert created["metadata"] == {"tag": "api"}
        assert (await runtime.sessions.get(created["id"])).agent_id == "writer"

        listing = await client.get("/api/v1/sessions?page=1&page_size=1")
        assert listing.status_code == 200
        assert listing.json()["page"] == {"number": 1, "size": 1, "has_more": True}
        assert len(listing.json()["data"]) == 1

        patched = await client.patch(
            f"/api/v1/sessions/{created['id']}", json={"title": "API research"}
        )
        assert patched.status_code == 200
        assert patched.json()["data"]["title"] == "API research"
        assert patched.json()["data"]["agent_id"] == "writer"


@pytest.mark.asyncio
async def test_session_context_and_deletion_use_resource_paths(tmp_path: Path) -> None:
    """Clearing or deleting a Session changes the real Runtime stores."""

    async with _api(tmp_path) as (client, runtime):
        created = await client.post(
            "/api/v1/sessions", json={"agent_id": "default", "title": "History"}
        )
        session_id = created.json()["data"]["id"]
        before_reset = await runtime.sessions.get(session_id)

        reset = await client.post(f"/api/v1/sessions/{session_id}/clear-context")
        assert reset.status_code == 200
        assert reset.json()["data"]["id"] == session_id
        assert (await runtime.sessions.get(session_id))._thread_id != before_reset._thread_id

        first_run = await client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"message_id": "context-first-0001", "content": "first"},
        )
        await _wait_for_run(runtime, first_run.json()["data"]["run_id"])
        second_run = await client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"message_id": "context-second-0002", "content": "second"},
        )
        await _wait_for_run(runtime, second_run.json()["data"]["run_id"])

        history = await client.get(f"/api/v1/sessions/{session_id}/events")
        assert history.status_code == 200
        assert [
            item["data"]["content"]
            for item in history.json()["data"]
            if item["type"] == "user/message"
        ] == ["first", "second"]

        deleted_session = await client.delete(f"/api/v1/sessions/{session_id}")
        assert deleted_session.status_code == 204
        assert await runtime.runs.list(session_id=session_id) == []
        missing = await client.get(f"/api/v1/sessions/{session_id}")
        assert missing.status_code == 404



@pytest.mark.asyncio
async def test_session_history_projects_inbox_input_only_after_claim(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, runtime):
        session_id = "inbox-history-truth"
        run_id = await _completed_run(client, runtime, session_id, "original")
        store = runtime.runs._store
        from bianbu_agent.sessions.inbox import SessionInboxMessage

        await store.admit_input(
            session_id,
            SessionInboxMessage(
                message_id="inbox-message-1",
                content="focus on the failure",
                created_at=10.0,
            ),
        )
        before = await client.get(f"/api/v1/sessions/{session_id}/events")
        assert [
            item["data"]["content"]
            for item in before.json()["data"]
            if item["type"] == "user/message"
        ] == ["original"]

        await store.append(
            run_id,
            RunEvent("input_applied", {"message_id": "inbox-message-1"}),
        )
        after = await client.get(f"/api/v1/sessions/{session_id}/events")
        assert [
            item["data"]["content"]
            for item in after.json()["data"]
            if item["type"] == "user/message"
        ] == [
            "original",
            "focus on the failure",
        ]





@pytest.mark.asyncio
async def test_legacy_session_history_message_and_title_paths_are_absent(tmp_path: Path) -> None:
    """The resource API must not retain parallel legacy Session operations."""

    old_operations = (
        ("GET", "/api/sessions"),
        ("POST", "/api/sessions"),
        ("GET", "/api/history?session_id=old"),
        ("POST", "/api/messages/delete"),
        ("POST", "/api/v1/sessions/old/clear_context"),
        ("POST", "/api/v1/sessions/old/generate_title"),
    )
    async with _api(tmp_path) as (client, _):
        responses = [await client.request(method, path) for method, path in old_operations]

    assert [response.status_code for response in responses] == [404] * len(responses)


@pytest.mark.asyncio
async def test_session_prompt_is_durable_idempotent_and_starts_an_idle_run(
    tmp_path: Path,
) -> None:
    """Lost responses must not duplicate a Session-owned message or its Run."""

    async with _api(tmp_path) as (client, runtime):
        await client.post(
            "/api/v1/sessions",
            json={"agent_id": "default", "title": "Prompt inbox"},
        )
        sessions = await runtime.sessions.list()
        session_id = sessions[-1].id
        payload = {
            "message_id": "message-idle-0001",
            "content": "hello from inbox",
            "attachments": [{"name": "brief.txt"}],
        }

        first = await client.post(
            f"/api/v1/sessions/{session_id}/prompt", json=payload
        )
        replay = await client.post(
            f"/api/v1/sessions/{session_id}/prompt", json=payload
        )
        conflict = await client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={**payload, "content": "different"},
        )
        skill_conflict = await client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={**payload, "skill_ids": ["global.skills/review"]},
        )

        assert first.status_code == replay.status_code == 202
        assert first.json() == replay.json()
        accepted = first.json()["data"]
        assert accepted["message_id"] == payload["message_id"]
        assert accepted["run_id"]
        await _wait_for_run(runtime, accepted["run_id"])
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "session_input_conflict"
        assert skill_conflict.status_code == 409
        assert skill_conflict.json()["error"]["code"] == "session_input_conflict"
        assert len(await runtime.runs.list(session_id=session_id)) == 1

        events = (
            await client.get(f"/api/v1/sessions/{session_id}/events")
        ).json()["data"]
        assert [
            event["data"].get("message_id")
            for event in events
            if event["type"] == "user/message"
        ] == ["message-idle-0001"]


@pytest.mark.asyncio
async def test_session_prompt_persists_selected_skill_ids(tmp_path: Path) -> None:
    """A global Skill selection must cross the HTTP and durable Run boundary."""

    async with _api(tmp_path) as (client, runtime):
        await runtime.sessions.get_or_create(session_id="selected-skill-input", agent_id="default")
        response = await client.post(
            "/api/v1/sessions/selected-skill-input/prompt",
            json={
                "message_id": "message-selected-skill",
                "content": "review this",
                "skill_ids": ["global/review"],
            },
        )

        assert response.status_code == 202
        record = await _wait_for_run(
            runtime,
            response.json()["data"]["run_id"],
            status="failed",
        )
        assert record.result is not None
        assert record.result.error is not None
        assert record.result.error.code == "skill_unavailable"
        [stored] = record.request["input_messages"]
        assert {
            key: value for key, value in stored.items() if key != "created_at"
        } == {
            "message_id": "message-selected-skill",
            "content": "review this",
            "attachments": [],
            "skill_ids": ["global/review"],
        }


@pytest.mark.asyncio
async def test_run_steer_route_is_removed(tmp_path: Path) -> None:
    async with _api(tmp_path) as (client, _runtime):
        response = await client.post(
            "/api/v1/runs/missing/steer",
            json={"instruction": "obsolete"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_session_owns_cancel_and_approval_controls(tmp_path: Path) -> None:
    async with _api(tmp_path) as (client, runtime):
        await runtime.sessions.get_or_create(session_id="approval-session", agent_id="default")
        started = await client.post(
            "/api/v1/sessions/approval-session/prompt",
            json={"message_id": "approval-message", "content": "interactive"},
        )
        run_id = started.json()["data"]["run_id"]
        for _ in range(100):
            record = await runtime.runs.get_record(run_id)
            if record is not None and any(
                event.kind == "approval_required" for event in record.events
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Session did not reach approval wait")

        await runtime.runs._store.start(
            "concurrent-title",
            "approval-session",
            {
                "prompt": "title",
                "metadata": {"console_operation": "generate_title"},
            },
        )

        approved = await client.post(
            "/api/v1/sessions/approval-session/approvals/approval-1",
            json={"decision": "approve", "reason": "safe"},
        )
        assert approved.status_code == 200
        assert approved.json() == {"data": {"accepted": True}}
        await _wait_for_run(runtime, run_id)
        await runtime.runs._store.finish(
            "concurrent-title",
            RunResult("concurrent-title", "cancelled"),
        )

        await runtime.sessions.get_or_create(session_id="cancel-session", agent_id="default")
        waiting = await client.post(
            "/api/v1/sessions/cancel-session/prompt",
            json={"message_id": "cancel-message", "content": "cancel"},
        )
        cancel_id = waiting.json()["data"]["run_id"]
        for _ in range(100):
            record = await runtime.runs.get_record(cancel_id)
            if record is not None and any(
                event.kind == "approval_required" for event in record.events
            ):
                break
            await asyncio.sleep(0.01)
        cancelled = await client.post("/api/v1/sessions/cancel-session/cancel")
        assert cancelled.status_code == 200
        await _wait_for_run(runtime, cancel_id, status="cancelled")

        assert (await client.post("/api/v1/runs/anything/cancel")).status_code == 404


@pytest.mark.asyncio
async def test_new_sessions_share_background_expert_warmup(tmp_path, monkeypatch):
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool, WorkerSupervisor
    started = asyncio.Event()
    release = asyncio.Event()
    launches = []

    async def slow_start(worker):
        launches.append(worker.agent_id)
        started.set()
        await release.wait()

    monkeypatch.setattr(WorkerSupervisor, 'start', slow_start)
    async with _api(tmp_path) as (client, runtime):
        pool = AgentWorkerPool(runtime.config)
        runtime.worker_supervisor = pool
        try:
            for sid in ('warmup-first', 'warmup-second'):
                response = await asyncio.wait_for(client.post('/api/v1/sessions', json={
                    'id': sid, 'agent_id': 'default'}), 1)
                assert response.status_code == 201
            await asyncio.sleep(0)
            assert started.is_set(), 'opening a session did not prepare its expert'
            assert launches == ['default']
            assert not release.is_set()
        finally:
            release.set()
            await pool.close()


# ── 错误契约穿过 HTTP 层 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_run_reaches_the_events_api_with_details(tmp_path: Path) -> None:
    """失败原因必须活着穿过 HTTP 层。

    客户端（Runtime WebUI 与 Bibit）都只从 `/sessions/{id}/events` 读错误；
    任何一层把 details/retryable 压扁，用户就只剩一句通用文案。
    """
    async with _api(tmp_path) as (client, runtime):
        session_id = "failed-run-contract"
        await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
        run_id = "failed-contract"
        store = runtime.runs._store
        await store.start(run_id, session_id, {"prompt": "deny me"})
        await store.finish(
            run_id,
            RunResult(
                request_id=run_id,
                status="failed",
                error=RunError(
                    code="security_denied",
                    message="[bianbu-security] 能力 network.access 未授权",
                    details={"tool": "web_search", "capability": "network.access",
                             "risk_level": "L2"},
                    retryable=False,
                ),
            ),
        )

        response = await client.get(f"/api/v1/sessions/{session_id}/events")

        assert response.status_code == 200
        terminal = next(
            item for item in response.json()["data"] if item["type"] == "run/failed"
        )
        assert terminal["data"]["error"] == {
            "code": "security_denied",
            "message": "[bianbu-security] 能力 network.access 未授权",
            "retryable": False,
            "details": {"tool": "web_search", "capability": "network.access",
                        "risk_level": "L2"},
        }
        # 终态快照与 error 字段一致：客户端不必判断该读哪一个。
        assert terminal["data"]["result"]["error"] == terminal["data"]["error"]


@pytest.mark.asyncio
async def test_mid_run_diagnostic_reaches_the_events_api(tmp_path: Path) -> None:
    """过程中的原因（上游 429）也要在 HTTP 载荷里，且与终态同形状。"""
    async with _api(tmp_path) as (client, runtime):
        session_id = "mid-run-diagnostic"
        await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
        run_id = "diagnostic-contract"
        store = runtime.runs._store
        await store.start(run_id, session_id, {"prompt": "rate limit me"})
        await store.append(run_id, RunEvent("error", {
            "code": "provider_rate_limited",
            "message": "上游返回 429：配额的请求数已耗尽",
            "retryable": True,
            "details": {"provider": "deepseek", "http_status": 429},
        }))
        await store.finish(
            run_id,
            RunResult(
                request_id=run_id,
                status="failed",
                error=RunError(code="run_failed", message="任务执行失败"),
            ),
        )

        response = await client.get(f"/api/v1/sessions/{session_id}/events")

        assert response.status_code == 200
        diagnostics = [
            item for item in response.json()["data"]
            if item["type"] == "run/event" and item["data"].get("kind") == "error"
        ]
        assert [item["data"]["payload"] for item in diagnostics] == [{
            "code": "provider_rate_limited",
            "message": "上游返回 429：配额的请求数已耗尽",
            "retryable": True,
            "details": {"provider": "deepseek", "http_status": 429},
        }]


@pytest.mark.asyncio
async def test_polled_session_history_reuses_the_rendered_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open Session is polled on an interval; only growth re-renders it."""

    async with _api(tmp_path) as (client, runtime):
        session_id = "polled-window"
        await runtime.sessions.get_or_create(
            session_id=session_id, agent_id="default"
        )
        store = runtime.runs._store
        await store.start("poll-run", session_id, {"prompt": "question"})
        await store.append("poll-run", RunEvent("token", {"text": "answer"}))
        await store.finish(
            "poll-run", RunResult("poll-run", "completed", "answer"),
        )

        renders = 0
        render_body = api_sessions._history_body

        def counting(events, max_messages, tail):
            nonlocal renders
            renders += 1
            return render_body(events, max_messages, tail)

        monkeypatch.setattr(api_sessions, "_history_body", counting)
        url = f"/api/v1/sessions/{session_id}/history?max_messages=50"

        first = await client.get(url)
        assert first.status_code == 200
        assert renders == 1

        second = await client.get(url)
        assert second.status_code == 200
        # 同一游标、同一 tail：轮询复用上一次渲染结果，不再解一遍 transcript。
        assert renders == 1
        assert second.content == first.content

        await store.start("poll-run-2", session_id, {"prompt": "again"})
        await store.append("poll-run-2", RunEvent("token", {"text": "more"}))
        await store.finish(
            "poll-run-2", RunResult("poll-run-2", "completed", "more"),
        )

        grown = await client.get(url)
        assert grown.status_code == 200
        # transcript 追加过事件，tail 变了，于是重新渲染并返回新内容。
        assert renders == 2
        assert grown.content != first.content
