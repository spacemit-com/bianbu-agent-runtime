import pytest
from types import SimpleNamespace
from bianbu_agent.context.config import CompressionSettingsProvider


def test_model_switch_recomputes_budget():
    provider = CompressionSettingsProvider()
    for capacity in (8192, 32768, 200000):
        model = SimpleNamespace(profile={"context_window": capacity}, max_tokens=1024)
        settings = provider.read(model)
        assert settings == CompressionSettingsProvider().read(model)
        assert settings.context_window_tokens == capacity
        assert settings.output_reserve == 1024
        assert settings.safety_reserve > 0
        assert settings.usable_input_tokens == capacity - 1024 - settings.safety_reserve
        assert settings.threshold_tokens == settings.usable_input_tokens
        assert settings.target_tokens == int(settings.usable_input_tokens * .5)
        assert 0 < settings.target_tokens < settings.threshold_tokens <= settings.usable_input_tokens
        assert settings.abort_on_summary_failure is True


def test_unknown_model_budget_is_explicitly_conservative():
    settings = CompressionSettingsProvider().read(None)
    assert settings.context_window_tokens <= 8192
    assert settings.output_reserve > 0
    assert settings.safety_reserve > 0
    assert settings.window_source == "fallback_conservative"


def test_compaction_keeps_system_and_complete_recent_turns():
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
    from bianbu_agent.context.compression import ContextCompactor
    settings = CompressionSettingsProvider().read(None).with_limits(
        threshold_tokens=256, target_tokens=128, protect_last_n=3,
    )
    messages = [
        SystemMessage(content="Never publish without authorization.", id="system"),
        HumanMessage(content="Earlier goal", id="old-user"),
        AIMessage(content="Old progress " * 100, id="old-answer"),
        HumanMessage(content="Current task: fix the package", id="current"),
        AIMessage(content="", tool_calls=[{"id": "call", "name": "check", "args": {}}]),
        ToolMessage(content="permission denied", tool_call_id="call", status="error"),
    ]
    plan = ContextCompactor().plan(messages, settings)
    assert plan is not None
    assert messages[0] in plan.head
    assert messages[0] not in plan.to_summarize
    assert [message.id for message in plan.tail][0] == "current"
    assert plan.tail[-1].tool_call_id == plan.tail[-2].tool_calls[0]["id"]
    assert plan.tail[-1].status == "error"


@pytest.mark.asyncio
async def test_summary_failure_leaves_original_history_unchanged():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import HumanMessage, AIMessage
    from bianbu_agent.context.middleware import ContextCompressionMiddleware, CompressionEventSink
    class FailingSummary(FakeMessagesListChatModel):
        def _generate(self, *args, **kwargs):
            raise RuntimeError("summary unavailable")
    model = FailingSummary(responses=[], profile={"max_input_tokens": 1000})
    messages = [HumanMessage(content="old " * 3000), AIMessage(content="old answer"), HumanMessage(content="current task")]
    original = [message.model_copy(deep=True) for message in messages]
    middleware = ContextCompressionMiddleware(model, CompressionSettingsProvider(), CompressionEventSink())
    assert await middleware.abefore_model({"messages": messages}, None) is None
    assert messages == original


def test_long_single_run_can_compact_older_tool_rounds_without_losing_current_request():
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
    from bianbu_agent.context.compression import ContextCompactor
    messages = [HumanMessage(content="Continue fixing the package; never publish.", id="current")]
    for i in range(20):
        messages.extend([
            AIMessage(content="", tool_calls=[{"id": f"call-{i}", "name": "check", "args": {}}]),
            ToolMessage(content="bounded work result", tool_call_id=f"call-{i}"),
        ])
    settings = CompressionSettingsProvider().read(None).with_limits(
        threshold_tokens=10, target_tokens=100, protect_last_n=4,
    )
    plan = ContextCompactor().plan(messages, settings)
    assert plan is not None
    retained = (*plan.head, *plan.tail)
    assert messages[0] in retained
    assert messages[0] not in plan.to_summarize
    assert len(plan.to_summarize) >= 20
    for group in (plan.to_summarize, plan.tail):
        calls = {call["id"] for message in group for call in getattr(message, "tool_calls", ())}
        results = {message.tool_call_id for message in group if isinstance(message, ToolMessage)}
        assert calls == results


def test_repeated_compaction_resummarizes_old_marker_instead_of_protecting_it():
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
    from bianbu_agent.context.compression import ContextCompactor, compression_metadata
    compactor = ContextCompactor()
    settings = CompressionSettingsProvider().read(None).with_limits(threshold_tokens=10, target_tokens=100, protect_last_n=2)
    messages = [HumanMessage(content="Keep working on the current task.", id="current")]
    for cycle in range(3):
        for i in range(10):
            call = f"{cycle}-{i}"
            messages.extend([AIMessage(content="", tool_calls=[{"id": call, "name": "check", "args": {}}]), ToolMessage(content="done", tool_call_id=call)])
        plan = compactor.plan(messages, settings)
        assert plan is not None
        assert all(compression_metadata(message) is None for message in plan.head)
        result = compactor.complete(plan, "Earlier checks passed; continue current task.", used_fallback=False)
        messages = list(result.messages)
        assert sum(compression_metadata(message) is not None for message in messages) == 1
        assert any(message.id == "current" for message in messages)
