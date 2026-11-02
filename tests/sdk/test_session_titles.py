from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from bianbu_agent.sessions.titles import (
    SessionTitleError,
    fallback_session_title,
    generate_session_title,
)
from bianbu_agent.models import OpenAICompatibleModel


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
async def test_title_request_drops_the_agent_reasoning_effort(monkeypatch) -> None:
    """A title is auxiliary and must not spend the Agent's reasoning budget."""

    captured: dict[str, object] = {}

    def to_langchain_model(model):
        captured["model"] = model
        return _TitleModel()

    monkeypatch.setattr(
        "bianbu_agent.sessions.titles.to_langchain_model",
        to_langchain_model,
    )

    await generate_session_title(
        OpenAICompatibleModel(
            model="reasoning-model",
            api_key="secret",
            base_url="https://model.test/v1",
            reasoning_effort="max",
        ),
        "hi",
    )

    model = captured["model"]
    assert isinstance(model, OpenAICompatibleModel)
    assert model.reasoning_effort == "low"


class _TitleModel:
    def bind(self, **_kwargs):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content="Title")


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


@pytest.mark.asyncio
async def test_title_request_masks_pii_before_the_provider_sees_it(
    monkeypatch,
) -> None:
    from bianbu_agent.security import security_runtime

    secret = "13824681357"
    scans: list[tuple[str, str]] = []

    def call(method, *args):
        if method == "get_agent_security_mode":
            return {"mode": "normal"}
        assert method == "dlp_scan"
        agent_id, text = args
        scans.append((agent_id, text))
        start = text.find(secret)
        return {"findings": [] if start < 0 else [dict(
            start=start,
            end=start + len(secret),
            pii_type="phone",
            action="mask",
        )]}

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setattr(security_runtime, "security_client_call", call)
    captured: dict[str, object] = {}

    class Model:
        def bind(self, **_kwargs):
            return self

        async def ainvoke(self, messages):
            captured["messages"] = messages
            return AIMessage(content="证件信息核对")

    monkeypatch.setattr(
        "bianbu_agent.sessions.titles.to_langchain_model",
        lambda _model: Model(),
    )

    title = await generate_session_title(
        object(),
        f"我的身份证是 {secret}",
        agent_id="agent.demo",
    )

    assert title == "证件信息核对"
    prompt = captured["messages"][1].content
    assert secret not in prompt
    # A natural stand-in replaced it, and the Session can resolve it back.
    from bianbu_agent.security.security_dlp import resolve_references

    restored, count = resolve_references(prompt)
    assert count == 1
    assert secret in restored
    assert scans
    assert all(agent_id == "agent.demo" for agent_id, _text in scans)


def test_fallback_title_respects_utf8_and_word_limits() -> None:
    assert fallback_session_title("one two three four five six") == (
        "one two three four five"
    )
    assert len(fallback_session_title("这是一个足够长的中文回退标题用于测试").encode()) <= 40
