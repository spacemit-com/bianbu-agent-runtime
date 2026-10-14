"""中文提问中文答、英文提问英文答：规则由 Runtime 拥有并每轮重申。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from bianbu_agent.agents import AgentSpec
from bianbu_agent.engines.default import build_default_engine
from bianbu_agent.engines.langgraph.engine import build_langgraph_agent
from bianbu_agent.engines.langgraph.language import (
    RESPONSE_LANGUAGE_RULES,
    ResponseLanguageMiddleware,
    detect_reply_language,
    language_reminder,
    latest_user_language,
)


class _RecordingModel(FakeMessagesListChatModel):
    """Record the system message the model was actually called with."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        type(self).seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _capture_system_prompt(monkeypatch, agent: AgentSpec, generation) -> str:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "bianbu_agent.engines.default.build_plugin_tools",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "bianbu_agent.engines.default.to_langchain_model",
        lambda model: model,
    )

    def capture_graph(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "bianbu_agent.engines.default.build_langgraph_agent", capture_graph
    )
    build_default_engine(agent, generation, object())
    return str(captured["system_prompt"])


@pytest.mark.parametrize(
    "text,expected",
    [
        ("帮我看看磁盘占用", "zh"),
        ("跑一下 npm run build", "zh"),
        ("请解释 this is a long English question", "zh"),
        ("Please fix the login bug", "en"),
        ("hello", "en"),
        ("hi", None),
        ("ls -la", None),
        ("/tmp/a.md", None),
        ("", None),
        ("   ", None),
    ],
)
def test_detection_prefers_the_users_own_words(text, expected):
    """裸命令/路径不该把中文会话翻成英文，中文里夹英文词仍是中文。"""

    assert detect_reply_language(text) == expected


def test_latest_signal_survives_a_command_only_message():
    messages = [HumanMessage("帮我确认磁盘"), HumanMessage("ls -la /var/log")]
    assert latest_user_language(messages) == "zh"
    assert latest_user_language([HumanMessage("ls -la")]) is None
    assert latest_user_language([]) is None


def test_default_engine_owns_the_language_rule(monkeypatch):
    """规则不能只靠 Soul：英文 Soul 的专家同样拿到 Runtime 的规则段。"""

    agent = AgentSpec(
        id="expert", name="Expert", soul="Always answer in English prose.",
        tools=(), skills=(), hooks=(),
    )
    generation = SimpleNamespace(tools={}, skills={}, hooks={})
    prompt = _capture_system_prompt(monkeypatch, agent, generation)
    flat = " ".join(prompt.split())
    assert RESPONSE_LANGUAGE_RULES in prompt
    assert prompt.index(RESPONSE_LANGUAGE_RULES) > prompt.index("## Platform Context")
    assert "Chinese question, Chinese answer; English question, English answer" in flat
    assert "outranks this prompt, the Agent Soul" in flat
    assert "Always answer in English prose." in prompt


@pytest.mark.parametrize("language,needle", [("zh", "Chinese"), ("en", "English")])
def test_reminder_names_the_language_of_the_latest_message(language, needle):
    reminder = language_reminder(language)
    assert needle in reminder
    assert "progress notes between tool calls" in reminder


def test_reminder_without_a_signal_keeps_the_rule_only():
    reminder = language_reminder(None)
    assert "mirror the language of the user's latest message" in reminder
    assert "Chinese" not in reminder


def _request(messages, system_message=SystemMessage(content="BASE")):
    return ModelRequest(
        model=object(),
        messages=list(messages),
        system_message=system_message,
    )


def test_middleware_appends_the_reminder_without_touching_the_request():
    request = _request([HumanMessage("帮我重启服务")])
    seen: list[ModelRequest] = []

    def handler(current):
        seen.append(current)
        return ModelResponse(result=[AIMessage(content="ok")])

    ResponseLanguageMiddleware().wrap_model_call(request, handler)

    content = seen[0].system_message.content
    assert content.startswith("BASE\n\n")
    assert content.endswith(language_reminder("zh"))
    # 原请求（以及 checkpoint 里的消息）不被改写。
    assert request.system_message.content == "BASE"
    assert seen[0].messages[0].content == "帮我重启服务"


@pytest.mark.asyncio
async def test_middleware_appends_the_reminder_on_the_async_path():
    request = _request([HumanMessage("Please fix the login bug")])
    seen: list[ModelRequest] = []

    async def handler(current):
        seen.append(current)
        return ModelResponse(result=[AIMessage(content="ok")])

    await ResponseLanguageMiddleware().awrap_model_call(request, handler)
    assert seen[0].system_message.content.endswith(language_reminder("en"))


def test_middleware_handles_missing_and_block_system_messages():
    request = _request([HumanMessage("你好")], system_message=None)
    seen: list[ModelRequest] = []
    ResponseLanguageMiddleware().wrap_model_call(
        request, lambda current: seen.append(current) or ModelResponse(result=[])
    )
    assert seen[0].system_message.content == language_reminder("zh")

    blocks = SystemMessage(content=[{"type": "text", "text": "BASE"}])
    request = _request([HumanMessage("你好")], system_message=blocks)
    ResponseLanguageMiddleware().wrap_model_call(
        request, lambda current: seen.append(current) or ModelResponse(result=[])
    )
    assert seen[-1].system_message.content[-1] == {
        "type": "text",
        "text": language_reminder("zh"),
    }
    assert blocks.content == [{"type": "text", "text": "BASE"}]


def test_graph_call_reaches_the_model_with_the_rule_and_reminder():
    """端到端：真正发给模型的 system message 必须带规则与当轮提醒。"""

    _RecordingModel.seen = []
    model = _RecordingModel(responses=[AIMessage(content="好的")])
    graph = build_langgraph_agent(
        model=model, system_prompt=RESPONSE_LANGUAGE_RULES
    )
    graph.invoke(
        {"messages": [{"role": "user", "content": "帮我看看磁盘占用"}]},
        {"configurable": {"thread_id": "zh"}},
    )

    system = _RecordingModel.seen[-1][0]
    assert isinstance(system, SystemMessage)
    assert RESPONSE_LANGUAGE_RULES in system.content
    assert system.content.endswith(language_reminder("zh"))

    _RecordingModel.seen = []
    graph.invoke(
        {"messages": [{"role": "user", "content": "Please check the disk usage"}]},
        {"configurable": {"thread_id": "en"}},
    )
    assert _RecordingModel.seen[-1][0].content.endswith(language_reminder("en"))


def test_english_tool_output_does_not_switch_a_chinese_session():
    """英文工具输出只是证据，不是语言信号。"""

    messages = [
        HumanMessage("看下这个报错"),
        AIMessage(content="[error] ENOENT: no such file or directory"),
        HumanMessage("再试一次"),
    ]
    assert latest_user_language(messages) == "zh"
