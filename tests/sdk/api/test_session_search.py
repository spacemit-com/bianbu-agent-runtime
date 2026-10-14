"""Conversation-text search: case-insensitive matching over real transcripts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bianbu_agent.api.sessions import _search_session
from bianbu_agent.sessions.search import search_folded
from bianbu_agent.runs import RunEvent
from bianbu_agent.sessions.events import (
    SessionEvent,
    session_event_for_inbox_user_message,
    session_event_for_run_event,
)
from bianbu_agent.sessions.inbox import SessionInboxMessage
from tests.sdk.api.test_runs import _api, _wait_terminal


async def _ask(client, runtime, session_id: str, content: str, *, message_id: str) -> str:
    """Drive one real Run so the transcript holds user text and assistant text."""

    await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
    response = await client.post(
        f"/api/v1/sessions/{session_id}/prompt",
        json={"message_id": message_id, "content": content},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    await _wait_terminal(runtime, run_id)
    return run_id


async def _search(client, query: str, **params: object) -> dict[str, object]:
    response = await client.get("/api/v1/sessions/search", params={"q": query, **params})
    assert response.status_code == 200, response.text
    return response.json()


async def _history_window(client, session_id: str, seq: int) -> dict[int, str]:
    window = await client.get(
        f"/api/v1/sessions/{session_id}/history",
        params={"before_seq": seq + 1, "max_messages": 50},
    )
    assert window.status_code == 200, window.text
    return {int(event["seq"]): str(event["type"]) for event in window.json()["data"]}


@pytest.mark.asyncio
async def test_search_matches_case_insensitively_and_reports_the_match_offset(tmp_path: Path) -> None:
    """A lowercase query finds mixed-case text and the snippet offsets select it."""

    async with _api(tmp_path) as (client, runtime):
        run_id = await _ask(
            client, runtime, "search-owner", "Bianbu Runtime 讨论记录", message_id="message-1",
        )

        body = await _search(client, "runtime")

        assert body["has_more"] is False
        assert body["query"] == "runtime"
        hits = body["data"]
        assert hits, "a lowercase query must match mixed-case conversation text"
        assert {hit["role"] for hit in hits} == {"user", "assistant"}
        for hit in hits:
            assert hit["session_id"] == "search-owner"
            assert hit["run_id"] == run_id
            matched = hit["snippet"][hit["match_start"] : hit["match_start"] + hit["match_length"]]
            assert matched.casefold() == "runtime"


@pytest.mark.asyncio
async def test_every_hit_anchor_is_a_message_the_history_window_can_scroll_to(tmp_path: Path) -> None:
    """A hit seq is the fold anchor of a visible message, never a mid-stream chunk."""

    async with _api(tmp_path) as (client, runtime):
        await _ask(client, runtime, "anchor-owner", "第一轮问题", message_id="message-1")
        await _ask(client, runtime, "anchor-owner", "第二轮问题 runtime 定位", message_id="message-2")

        hits = (await _search(client, "runtime"))["data"]
        assert hits
        assistant = next(hit for hit in hits if hit["role"] == "assistant")
        assert assistant["end_seq"] >= assistant["seq"]

        expected_type = {"user": "user/message", "assistant": "assistant/text.delta"}
        for hit in hits:
            window = await _history_window(client, "anchor-owner", hit["end_seq"])
            assert hit["seq"] in window, "the anchor must survive the fold the reader renders"
            assert window[hit["seq"]] == expected_type[hit["role"]]
        # 同一会话内命中按 seq 倒序，最新的一条排在最前。
        assert [hit["seq"] for hit in hits] == sorted((hit["seq"] for hit in hits), reverse=True)

        # 客户端按 end_seq 读窗口：助手回答是完整的，不会被截在第一个分片。
        folded = await client.get(
            "/api/v1/sessions/anchor-owner/history",
            params={"before_seq": assistant["end_seq"] + 1, "max_messages": 50},
        )
        assert folded.status_code == 200
        last = folded.json()["data"][-1]
        assert last["type"] == "assistant/text.delta"
        assert last["seq"] == assistant["seq"]
        assert "runtime 定位" in last["data"]["text"]


def test_hit_reports_the_folded_group_end_so_the_window_is_complete() -> None:
    """一次回答被折成一条时，命中必须带出整组覆盖的末尾 seq。"""

    folded = [
        {
            "seq": 1,
            "type": "assistant/text.delta",
            "run_id": "run-1",
            "data": {"text": "第一段 zebra 第二段结论", "seq_range": [1, 9]},
        }
    ]

    hits = search_folded(folded, "zebra", limit=5)

    assert len(hits) == 1
    assert hits[0]["seq"] == 1
    assert hits[0]["end_seq"] == 9


def test_search_session_hides_runs_the_conversation_view_hides() -> None:
    """Text of a hidden console operation never becomes a search hit."""

    hidden = session_event_for_inbox_user_message(
        "unit-session",
        "title-run",
        SessionInboxMessage(
            message_id="message-hidden", content="隐藏标题文本 zzz-hidden", attachments=(), skill_ids=(),
        ),
        seq=0,
        event_time=0.0,
    )
    hidden = SessionEvent(
        hidden.session_id,
        hidden.seq,
        hidden.type,
        hidden.time,
        hidden.run_id,
        {**dict(hidden.data), "console_operation": "generate_title"},
    )
    events: list[object] = [
        hidden,
        session_event_for_inbox_user_message(
            "unit-session",
            "run-1",
            SessionInboxMessage(
                message_id="message-1", content="可见问题", attachments=(), skill_ids=(),
            ),
            seq=1,
            event_time=1.0,
        ),
        session_event_for_run_event(
            "unit-session",
            "run-1",
            RunEvent("token", {"text": "回答里提到 zzz-visible"}),
            seq=2,
            event_time=2.0,
        ),
    ]

    assert _search_session(events, "zzz-hidden", 20) == []
    assert [hit["role"] for hit in _search_session(events, "zzz-visible", 20)] == ["assistant"]
    assert [hit["role"] for hit in _search_session(events, "可见", 20)] == ["user"]


@pytest.mark.asyncio
async def test_search_orders_sessions_by_recency_and_reports_truncation(tmp_path: Path) -> None:
    """Results follow Session recency and stop at the requested limit."""

    async with _api(tmp_path) as (client, runtime):
        await _ask(client, runtime, "older-session", "共用关键词 alpha", message_id="message-1")
        await asyncio.sleep(0.01)
        await _ask(client, runtime, "newer-session", "共用关键词 alpha", message_id="message-1")

        both = await _search(client, "alpha")
        assert {hit["session_id"] for hit in both["data"]} == {"older-session", "newer-session"}
        updated = [hit["updated_at"] for hit in both["data"]]
        assert updated == sorted(updated, reverse=True)

        limited = await _search(client, "alpha", limit=1)
        assert len(limited["data"]) == 1
        assert limited["has_more"] is True


@pytest.mark.asyncio
async def test_search_requires_a_query(tmp_path: Path) -> None:
    """An empty or blank query is rejected instead of listing every message."""

    async with _api(tmp_path) as (client, _runtime):
        response = await client.get("/api/v1/sessions/search", params={"q": ""})
        assert response.status_code == 422
        blank = await client.get("/api/v1/sessions/search", params={"q": "   "})
        assert blank.status_code == 422
