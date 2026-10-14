from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bianbu_agent.context.compression import ContextCompactor
from bianbu_agent.context.config import CompressionSettingsProvider


def test_settings_budget_the_selected_model_profile() -> None:
    model = SimpleNamespace(
        profile={"context_window": 200_000, "max_output_tokens": 8_000}
    )

    settings = CompressionSettingsProvider().read(model)

    assert settings.context_window_tokens == 200_000
    assert settings.output_reserve == 8_000
    assert settings.safety_reserve == 10_000
    assert settings.usable_input_tokens == 182_000
    assert settings.threshold_tokens == 182_000
    assert settings.target_tokens == 91_000
    assert settings.protect_first_n == 0
    assert settings.protect_last_n == 12
    assert settings.window_source == "profile"


def test_settings_reread_model_profile_then_conservative_fallback() -> None:
    provider = CompressionSettingsProvider()
    model = SimpleNamespace(
        profile={"context_window": 200_000, "max_output_tokens": 8_000}
    )

    inferred = provider.read(model)
    fallback = provider.read(SimpleNamespace(profile={}))

    assert (inferred.usable_input_tokens, inferred.window_source) == (
        182_000,
        "profile",
    )
    assert (fallback.usable_input_tokens, fallback.threshold_tokens) == (
        5_734,
        5_734,
    )


def test_compaction_plan_never_splits_assistant_tool_result_group() -> None:
    settings = CompressionSettingsProvider().read(None)
    settings = settings.with_limits(
        usable_input_tokens=800,
        threshold_tokens=640,
        target_tokens=400,
        protect_first_n=1,
        protect_last_n=3,
    )
    tool_ai = AIMessage(
        content="",
        id="assistant-tool",
        tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
    )
    tool_result = ToolMessage(
        content="result",
        id="tool-result",
        name="lookup",
        tool_call_id="call-1",
    )
    messages = [
        HumanMessage(content="first", id="first"),
        AIMessage(content="old answer " * 400, id="old-answer"),
        tool_ai,
        tool_result,
    ]

    plan = ContextCompactor().plan(messages, settings)

    assert plan is not None
    assert [message.id for message in plan.head] == ["first"]
    assert [message.id for message in plan.tail] == [
        "assistant-tool",
        "tool-result",
    ]
    assert [message.id for message in plan.to_summarize] == ["old-answer"]


def test_compaction_threshold_includes_static_system_and_tool_schema_tokens() -> None:
    settings = CompressionSettingsProvider().read(None).with_limits(
        usable_input_tokens=800,
        threshold_tokens=640,
        target_tokens=400,
        protect_first_n=0,
        protect_last_n=3,
    )
    messages = [
        HumanMessage(content="old context", id="old"),
        AIMessage(content="old answer" * 150, id="answer"),
        HumanMessage(content="new request", id="new"),
    ]

    without_static = ContextCompactor().plan(messages, settings)
    with_static = ContextCompactor().plan(messages, settings, static_tokens=300)

    assert without_static is None
    assert with_static is not None
    assert with_static.before_tokens >= 640


def test_emergency_replacement_summarizes_old_tool_content_without_copying_it() -> None:
    settings = CompressionSettingsProvider().read(None).with_limits(
        threshold_tokens=1,
        target_tokens=8,
        protect_first_n=0,
        protect_last_n=1,
    )
    secret_payload = "BASE64_PAYLOAD_" * 5_000
    messages = [
        HumanMessage(content="inspect report", id="request"),
        AIMessage(
            content="",
            id="tool-call",
            tool_calls=[{"id": "media", "name": "read_media", "args": {}}],
        ),
        ToolMessage(
            content=secret_payload,
            id="media-result",
            name="read_media",
            tool_call_id="media",
        ),
        HumanMessage(content="continue", id="continue"),
    ]

    result = ContextCompactor().compact_emergency(messages, settings)

    assert result is not None
    serialized = "\n".join(str(message.content) for message in result.messages)
    assert secret_payload not in serialized
    assert "read_media" in serialized
    assert "75,000 chars" in serialized
    assert result.after_tokens < result.before_tokens


def test_oversized_latest_tool_group_keeps_causality_but_omits_result_body() -> None:
    settings = CompressionSettingsProvider().read(None).with_limits(
        usable_input_tokens=800,
        threshold_tokens=640,
        target_tokens=400,
        protect_first_n=0,
        protect_last_n=3,
    )
    payload = "large-result-" * 1_000
    messages = [
        HumanMessage(content="request", id="request"),
        AIMessage(
            content="",
            id="tool-call",
            tool_calls=[{"id": "call", "name": "lookup", "args": {}}],
        ),
        ToolMessage(
            content=payload,
            id="tool-result",
            name="lookup",
            tool_call_id="call",
        ),
    ]

    plan = ContextCompactor().plan(messages, settings)

    assert plan is not None
    assert [message.id for message in plan.tail] == [
        "request",
        "tool-call",
        "tool-result",
    ]
    assert payload not in str(plan.tail[-1].content)
    assert "13,000 chars" in str(plan.tail[-1].content)
    assert plan.tail[-1].tool_call_id == "call"


def test_current_multi_part_user_input_is_never_moved_into_summary() -> None:
    settings = CompressionSettingsProvider().read(None).with_limits(
        usable_input_tokens=800,
        threshold_tokens=640,
        target_tokens=400,
        protect_first_n=0,
        protect_last_n=1,
    )
    attachment = "attachment-metadata-" * 100
    messages = [
        HumanMessage(content="old request", id="old-request"),
        AIMessage(content="old answer" * 200, id="old-answer"),
        HumanMessage(content=attachment, id="current:prompt:0"),
        HumanMessage(content="continue the report", id="current:prompt:1"),
    ]

    plan = ContextCompactor().plan(messages, settings)

    assert plan is not None
    assert [message.id for message in plan.tail] == [
        "current:prompt:0",
        "current:prompt:1",
    ]
    assert attachment not in "\n".join(
        str(message.content) for message in plan.to_summarize
    )


def test_current_user_intent_stays_with_its_latest_tool_round() -> None:
    settings = CompressionSettingsProvider().read(None).with_limits(
        usable_input_tokens=800,
        threshold_tokens=640,
        target_tokens=400,
        protect_first_n=0,
        protect_last_n=1,
    )
    payload = "latest-tool-result-" * 1_000
    messages = [
        HumanMessage(content="old request", id="old-request"),
        AIMessage(content="old answer", id="old-answer"),
        HumanMessage(content="build the report", id="current"),
        AIMessage(
            content="",
            id="latest-tool-call",
            tool_calls=[{"id": "latest", "name": "render", "args": {}}],
        ),
        ToolMessage(
            content=payload,
            id="latest-tool-result",
            name="render",
            tool_call_id="latest",
        ),
    ]

    plan = ContextCompactor().plan(messages, settings)

    assert plan is not None
    assert "current" in [message.id for message in plan.tail]
    assert "current" not in [message.id for message in plan.to_summarize]
    assert payload not in str(plan.tail[-1].content)


def test_token_estimate_includes_retained_provider_reasoning():
    compactor = ContextCompactor()
    plain = AIMessage(content="next action")
    reasoning = plain.model_copy(update={"additional_kwargs": {
        "reasoning_content": "r" * 4000}})
    assert compactor.estimate_tokens([reasoning]) >= compactor.estimate_tokens([plain]) + 1000


def test_large_summary_leaves_room_before_next_compaction():
    settings = CompressionSettingsProvider().read(None)
    compactor = ContextCompactor()
    messages = [HumanMessage(content="old request", id="old"),
                AIMessage(content="history " * 2200, id="answer"),
                HumanMessage(content="summarize this conversation", id="current")]
    plan = compactor.plan(messages, settings, static_tokens=1800)
    assert plan is not None
    result = compactor.complete(plan, "verbose summary " * 3000, used_fallback=False)
    assert result.after_tokens <= settings.target_tokens
    assert compactor.plan(result.messages, settings, static_tokens=1800) is None
    assert any(m.id == "current" for m in result.messages)


def test_large_fixed_prompt_compacts_once_without_rewriting_same_summary():
    settings = CompressionSettingsProvider().read(None)
    compactor = ContextCompactor()
    messages = [HumanMessage(content="old request", id="old"),
                AIMessage(content="history " * 800, id="answer"),
                HumanMessage(content="summarize this conversation", id="current")]
    plan = compactor.plan(messages, settings, static_tokens=4500)
    assert plan is not None
    result = compactor.complete(plan, "verbose summary " * 3000, used_fallback=False)
    assert result.after_tokens < settings.usable_input_tokens - 200
    assert compactor.plan(result.messages, settings, static_tokens=4500) is None
