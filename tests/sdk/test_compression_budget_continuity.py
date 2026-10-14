import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bianbu_agent.context.compression import ContextCompactor, deterministic_summary
from bianbu_agent.context.config import CompressionSettingsProvider


def history(rounds=20):
    messages = [HumanMessage(content="Port MaskedVByte and test it; preserve existing files.")]
    for i in range(rounds):
        messages.extend([
            AIMessage(content="", tool_calls=[{"id": str(i), "name": "exec", "args": {"cmd": f"edit /repo/src/port_{i}.c"}}]),
            ToolMessage(content=f"Changed /repo/src/port_{i}.c; test case bug_{i} failed with SIGSEGV\n" + "x" * 1800, tool_call_id=str(i)),
        ])
    return messages


def test_static_prompt_and_current_task_are_deducted_from_retained_history_budget():
    settings = CompressionSettingsProvider().read(None)
    compactor = ContextCompactor()
    messages = history()
    plan = compactor.plan(messages, settings, static_tokens=settings.usable_input_tokens - 1200)
    assert plan is not None
    result = compactor.complete(plan, "Earlier work summarized.", used_fallback=False)
    assert result.after_tokens <= settings.usable_input_tokens
    assert messages[0] in result.messages
    assert result.after_tokens < result.before_tokens
    retained_calls = {call["id"] for message in result.messages for call in getattr(message, "tool_calls", ())}
    results = {message.tool_call_id for message in result.messages if isinstance(message, ToolMessage)}
    assert retained_calls == results


def test_emergency_reclaims_space_from_recent_medium_sized_results():
    settings = CompressionSettingsProvider().read(None)
    compactor = ContextCompactor()
    messages = history(6)
    result = compactor.compact_emergency(messages, settings, static_tokens=settings.usable_input_tokens - 1200)
    assert result is not None
    assert result.after_tokens <= settings.usable_input_tokens
    assert messages[0] in result.messages


def test_summary_input_preserves_commands_paths_and_failed_process_output():
    digest = deterministic_summary(history())
    assert "edit /repo/src/port_0.c" in digest
    assert "SIGSEGV" in digest
    assert "bug_0" in digest
    assert "preserve existing files" in digest
    assert len(digest) <= 6000
    assert "x" * 1800 not in digest


@pytest.mark.asyncio
async def test_summary_model_receives_meaningful_bounded_tool_evidence():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from bianbu_agent.context.middleware import ContextCompressionMiddleware, CompressionEventSink

    class RecordingModel(FakeMessagesListChatModel):
        prompts: list[str] = []

        def _generate(self, messages, *args, **kwargs):
            self.prompts.append(str(messages[0].content))
            return super()._generate(messages, *args, **kwargs)

    model = RecordingModel(responses=[AIMessage(content="Keep the failed build evidence.")],
                           profile={"max_input_tokens": 8192})
    middleware = ContextCompressionMiddleware(
        model, CompressionSettingsProvider(), CompressionEventSink()
    )
    assert await middleware.abefore_model({"messages": history()}, None) is not None
    assert len(model.prompts) == 1
    prompt = model.prompts[0]
    assert "edit /repo/src/port_0.c" in prompt
    assert "SIGSEGV" in prompt
    assert "bug_0" in prompt
    assert "x" * 1800 not in prompt
    assert "completed tool call can contain a failed command" in prompt


def test_repeated_long_run_compaction_retains_initial_command_and_failure_evidence():
    settings = CompressionSettingsProvider().read(None)
    compactor = ContextCompactor()
    messages = history()
    for cycle in range(3):
        plan = compactor.plan(messages, settings, force=True)
        assert plan is not None
        digest = deterministic_summary(plan.to_summarize)
        assert "edit /repo/src/port_0.c" in digest
        assert "SIGSEGV" in digest
        result = compactor.complete(plan, digest, used_fallback=True)
        assert result.after_tokens <= settings.usable_input_tokens
        assert any("preserve existing files" in str(message.content) for message in result.messages)
        messages = list(result.messages)
        for i in range(10):
            call_id = f"next-{cycle}-{i}"
            messages.extend([
                AIMessage(content="", tool_calls=[{"id": call_id, "name": "exec",
                                                   "args": {"cmd": f"check /repo/next_{i}.c"}}]),
                ToolMessage(content="new check passed\n" + "x" * 1800, tool_call_id=call_id),
            ])


def test_large_code_write_arguments_can_be_summarized_without_losing_result():
    settings = CompressionSettingsProvider().read(None)
    request = HumanMessage(content="Create chess app")
    messages = [request, AIMessage(content="", tool_calls=[{
        "id": "write", "name": "write_file", "args": {
            "path": "app.html", "content": "<div>棋</div>" * 10000,
        },
    }]), ToolMessage(content="CRITICAL test failed SIGSEGV\n" + "payload " * 4000,
                    tool_call_id="write")]
    compactor = ContextCompactor()
    plan = compactor.plan(messages, settings, static_tokens=100)
    assert plan is not None
    digest = deterministic_summary(plan.to_summarize)
    assert "SIGSEGV" in digest
    result = compactor.complete(plan, digest, used_fallback=True)
    assert request in result.messages
    assert result.after_tokens <= settings.usable_input_tokens
    assert compactor.compact_emergency(messages, settings, static_tokens=100) is not None


def test_oversized_current_request_is_preserved_without_futile_compaction():
    settings = CompressionSettingsProvider().read(None)
    request = HumanMessage(content="任务" * settings.context_window_tokens)
    messages = [request, AIMessage(content="previous work")]
    assert ContextCompactor().compact_emergency(messages, settings) is None
    assert messages[0] is request


def test_latest_retained_tool_excerpt_keeps_process_failure_evidence():
    settings = CompressionSettingsProvider().read(None)
    messages = [HumanMessage(content="Keep going."), AIMessage(content="", tool_calls=[{
        "id": "test", "name": "exec", "args": {"cmd": "make test"},
    }]), ToolMessage(content="CRITICAL latest test failed SIGSEGV\n" + "payload " * 4000,
                    tool_call_id="test")]
    compactor = ContextCompactor()
    plan = compactor.plan(messages, settings, static_tokens=100)
    assert plan is not None
    result = compactor.complete(plan, deterministic_summary(plan.to_summarize), used_fallback=True)
    text = "\n".join(str(message.content) for message in result.messages)
    assert "SIGSEGV" in text
    assert "latest test failed" in text
    assert "payload " * 4000 not in text
    assert result.after_tokens <= settings.usable_input_tokens
    calls = {call["id"] for message in result.messages for call in getattr(message, "tool_calls", ())}
    results = {message.tool_call_id for message in result.messages if isinstance(message, ToolMessage)}
    assert calls == results
