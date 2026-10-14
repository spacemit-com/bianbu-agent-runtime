from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from bianbu_agent.sessions.titles import (
    SessionTitleError,
    fallback_session_title,
    generate_session_title,
)


@pytest.mark.asyncio
async def test_title_request_is_bounded_and_isolated(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Model:
        def bind(self, **kwargs):
            captured["bind"] = kwargs
            return self

        async def ainvoke(self, messages):
            captured["messages"] = messages
            return AIMessage(content="K3 服务检查")

    monkeypatch.setattr(
        "bianbu_agent.sessions.titles.to_langchain_model",
        lambda _model: Model(),
    )

    title = await generate_session_title(object(), "检查 K3 服务")

    assert title == "K3 服务检查"
    assert captured["bind"] == {"max_tokens": 64}
    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert '"text":"检查 K3 服务"' in messages[1].content


@pytest.mark.asyncio
async def test_title_request_times_out_instead_of_remaining_active(
    monkeypatch,
) -> None:
    class Model:
        def bind(self, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "bianbu_agent.sessions.titles.to_langchain_model",
        lambda _model: Model(),
    )

    with pytest.raises(SessionTitleError, match="timed out"):
        await generate_session_title(object(), "hi", timeout_seconds=0.001)


@pytest.mark.asyncio
async def test_title_request_rejects_tool_calls(monkeypatch) -> None:
    class Model:
        def bind(self, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            return AIMessage(
                content="",
                tool_calls=[{"name": "shell", "args": {}, "id": "call-1"}],
            )

    monkeypatch.setattr(
        "bianbu_agent.sessions.titles.to_langchain_model",
        lambda _model: Model(),
    )

    with pytest.raises(SessionTitleError, match="requested a tool"):
        await generate_session_title(object(), "hi")


def _static_model(monkeypatch, content: str) -> None:
    class Model:
        def bind(self, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            return AIMessage(content=content)

    monkeypatch.setattr(
        "bianbu_agent.sessions.titles.to_langchain_model",
        lambda _model: Model(),
    )


@pytest.mark.asyncio
async def test_title_request_rejects_reasoning_only_answer(monkeypatch) -> None:
    """Truncated MiniMax reasoning must not become the visible Session title."""

    _static_model(monkeypatch, "<think>reasoning that never closes")

    with pytest.raises(SessionTitleError, match="no text"):
        await generate_session_title(object(), "hi")


@pytest.mark.asyncio
async def test_title_request_keeps_text_after_reasoning_markup(monkeypatch) -> None:
    """MiniMax streams reasoning in-band; the title is the visible answer."""

    _static_model(
        monkeypatch,
        "<think>The user asks about the K3 service.\n</think>\n\nK3 服务检查",
    )

    assert await generate_session_title(object(), "检查 K3 服务") == "K3 服务检查"


@pytest.mark.asyncio
async def test_title_request_tolerates_control_characters(monkeypatch) -> None:
    _static_model(monkeypatch, "<think>why\n</think>\n\x07K3 服务检查\x00")

    assert await generate_session_title(object(), "检查 K3 服务") == "K3 服务检查"


@pytest.mark.asyncio
async def test_title_request_rejects_remaining_markup(monkeypatch) -> None:
    _static_model(monkeypatch, "<b>K3 服务检查</b>")

    with pytest.raises(SessionTitleError, match="markup"):
        await generate_session_title(object(), "检查 K3 服务")


def test_fallback_title_respects_utf8_and_word_limits() -> None:
    assert fallback_session_title("one two three four five six") == (
        "one two three four five"
    )
    assert len(fallback_session_title("这是一个足够长的中文回退标题用于测试").encode()) <= 40
