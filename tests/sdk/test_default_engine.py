from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk

from bianbu_agent.agents import AgentModel, AgentSpec
from bianbu_agent.engines.default import build_default_engine, to_langchain_model
from bianbu_agent.models import (
    OpenAICompatibleModel,
    ProviderConfig,
    YAMLModelResolver,
    YAMLModelStore,
)


def _capture_system_prompt(
    monkeypatch,
    agent: AgentSpec,
    generation,
    *,
    model_tool_names: tuple[str, ...] = (),
    model=None,
) -> str:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "bianbu_agent.engines.default.build_plugin_tools",
        lambda *args, **kwargs: [
            SimpleNamespace(name=name) for name in model_tool_names
        ],
    )
    monkeypatch.setattr(
        "bianbu_agent.engines.default.to_langchain_model",
        lambda model: model,
    )

    def capture_graph(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "bianbu_agent.engines.default.build_langgraph_agent",
        capture_graph,
    )
    build_default_engine(agent, generation, model if model is not None else object())
    return str(captured["system_prompt"])


@pytest.mark.parametrize("architecture", ["riscv64", "x86_64", ""])
def test_new_expert_knows_platform_without_selected_capabilities(monkeypatch, architecture):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: architecture)
    monkeypatch.setenv("BIBIT_API_KEY", "must-not-enter-prompt")
    agent = AgentSpec(
        id="com.bianbu.012345abcdef", name="New Expert", soul="Keep my specialty.",
        tools=(), skills=(), hooks=(),
    )
    generation = SimpleNamespace(tools={}, skills={}, hooks={})
    prompts = [_capture_system_prompt(monkeypatch, agent, generation) for _ in range(2)]
    assert prompts[0] == prompts[1]
    for prompt in prompts:
        assert prompt.count("## Platform Context") == 1
        assert "Bibit" in prompt
        assert "Bianbu Agent Runtime" in prompt
        assert "Agent ID: com.bianbu.012345abcdef" in prompt
        assert "Operating system: Linux" in prompt
        assert f"Architecture: {architecture or 'Unknown'}" in prompt
        assert "Keep my specialty." in prompt
        assert "must-not-enter-prompt" not in prompt
        assert "does not grant tools, Skills, or permissions" in prompt


def test_platform_context_leaves_os_guidance_to_selected_platform_hook(monkeypatch):
    agent = AgentSpec(id="expert", name="Expert", hooks=("platform.context",))
    generation = SimpleNamespace(tools={}, skills={}, hooks={"platform.context": object()})
    prompt = _capture_system_prompt(monkeypatch, agent, generation)
    assert "Bibit" in prompt
    assert "Architecture:" in prompt
    assert "Operating system:" not in prompt


@pytest.mark.parametrize("tool_names", [(), ("terminal", "pkg_install")])
def test_software_request_boundaries_apply_without_optional_hooks(monkeypatch, tool_names):
    agent = AgentSpec(id="expert", name="Expert", hooks=())
    generation = SimpleNamespace(tools={}, skills={}, hooks={})
    prompt = _capture_system_prompt(monkeypatch, agent, generation, model_tool_names=tool_names)
    assert "A download request (下载) authorizes fetching" in prompt
    assert "not installing, upgrading, configuring, repairing, removing, or running" in prompt
    assert "does not expand that authorization" in prompt
    assert "never install tools or installer scripts" in prompt
    assert "authorized host installation tool" in prompt
    assert "fall back to terminal sudo/nsenter" in prompt
    assert "private /tmp is not proof of delivery to the host" in prompt


@pytest.mark.parametrize("tool_names", [(), ("terminal", "pkg_install")])
def test_host_worker_is_told_it_is_not_sandboxed(monkeypatch, tool_names):
    """A host Worker must not be handed the sandbox rules.

    The old text told every session "do not fall back to terminal sudo/nsenter",
    which a host Agent read as "I am sandboxed and may not elevate", so it
    reported installs and reboots as impossible (K3 session on 2026-09-23).
    """

    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "0")
    agent = AgentSpec(id="expert", name="Expert", hooks=())
    generation = SimpleNamespace(tools={}, skills={}, hooks={})
    prompt = _capture_system_prompt(
        monkeypatch, agent, generation, model_tool_names=tool_names)
    assert "Execution mode: host" in prompt
    assert "not inside a container or a bwrap sandbox" in prompt
    assert "ask the user for the password" in prompt
    assert "sudo -S" in prompt
    assert "fall back to terminal sudo/nsenter" not in prompt
    assert "disable sandbox protections" not in prompt
    assert "private /tmp is not proof of delivery to the host" not in prompt


@pytest.mark.parametrize("tool_names", [(), ("terminal", "pkg_install")])
def test_sandbox_worker_keeps_the_sandbox_boundaries(monkeypatch, tool_names):
    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "1")
    agent = AgentSpec(id="expert", name="Expert", hooks=())
    generation = SimpleNamespace(tools={}, skills={}, hooks={})
    prompt = _capture_system_prompt(
        monkeypatch, agent, generation, model_tool_names=tool_names)
    assert "Execution mode: sandbox" in prompt
    assert "fall back to terminal sudo/nsenter" in prompt
    assert "not inside a container or a bwrap sandbox" not in prompt
    assert "ask the user for the password" not in prompt


def test_default_engine_includes_complete_agent_profile_before_skills(monkeypatch):
    agent = AgentSpec(
        id="dancer",
        name="dancer",
        description="喜欢跳舞",
        soul="喜欢跳舞",
        skills=("rhythm",),
    )
    generation = SimpleNamespace(
        tools={},
        skills={
            "rhythm": SimpleNamespace(
                spec=SimpleNamespace(
                    display_name="Rhythm",
                    description="Keep rhythm.",
                    instructions="Keep time.",
                )
            )
        },
        policy=object(),
        audit=object(),
    )

    system_prompt = _capture_system_prompt(monkeypatch, agent, generation)

    body = system_prompt.split("\n\n## Platform Context", 1)[0]
    assert body.startswith(
        "## Agent Profile\n"
        "\n"
        "Name: dancer\n"
        "Model: unknown\n"
        "Model provider ID: unknown\n"
        "\n"
        "## Agent Soul\n"
        "\n"
        "喜欢跳舞\n"
        "\n"
        "## Power Dependencies\n"
    )
    assert body.endswith(
        "## Tool Policy\n"
        "\n"
            "Available tool names: none. Never call or invent a tool that is not "
        "in this list.\n"
        "\n"
        "## Skill: Rhythm\n"
        "\n"
        "Keep rhythm."
    )
    assert (
        body.index("## Agent Profile")
        < body.index("## Agent Soul")
        < body.index("## Power Dependencies")
        < body.index("## Tool Policy")
        < body.index("## Skill: Rhythm")
    )


def test_default_engine_states_the_skills_the_user_attached(monkeypatch):
    """An attached Skill is a request, not background context to improvise past."""

    agent = AgentSpec(
        id="dancer",
        name="dancer",
        skills=("rhythm", "spacemit.power/pipeline"),
        requested_skills=("spacemit.power/pipeline",),
    )
    generation = SimpleNamespace(
        tools={},
        skills={
            "rhythm": SimpleNamespace(
                spec=SimpleNamespace(
                    display_name="Rhythm",
                    description="Keep rhythm.",
                    instructions="",
                )
            ),
            "spacemit.power/pipeline": SimpleNamespace(
                spec=SimpleNamespace(
                    display_name="Pipeline",
                    description="Render video.",
                    instructions="",
                )
            ),
        },
        policy=object(),
        audit=object(),
    )

    system_prompt = _capture_system_prompt(monkeypatch, agent, generation)

    assert "## Skill: Rhythm\n\nKeep rhythm." in system_prompt
    assert "## User-Requested Skills" in system_prompt
    assert "### Pipeline (spacemit.power/pipeline)\n\nRender video." in system_prompt
    # Stated once, as the requested procedure, never as ambient Agent context.
    assert "## Skill: Pipeline" not in system_prompt
    assert system_prompt.count("Render video.") == 1


def test_default_engine_emits_name_only_profile_when_optional_fields_are_empty(
    monkeypatch,
):
    agent = AgentSpec(id="dancer", name="dancer")
    generation = SimpleNamespace(
        tools={},
        skills={},
        policy=object(),
        audit=object(),
    )

    system_prompt = _capture_system_prompt(monkeypatch, agent, generation)

    body = system_prompt.split("\n\n## Platform Context", 1)[0]
    assert body.startswith(
        "## Agent Profile\n"
        "\n"
        "Name: dancer\n"
        "Model: unknown\n"
        "Model provider ID: unknown\n"
        "\n"
        "## Power Dependencies\n"
    )
    assert body.endswith(
        "## Tool Policy\n"
        "\n"
            "Available tool names: none. Never call or invent a tool that is not "
        "in this list."
    )


def test_default_engine_system_prompt_forbids_unbound_tool_names(monkeypatch):
    agent = AgentSpec(
        id="browser-only",
        name="Browser Only",
        tools=("mcp.browser/browser_navigate",),
    )
    generation = SimpleNamespace(
        tools={"mcp.browser/browser_navigate": object()},
        skills={},
        policy=object(),
        audit=object(),
    )

    system_prompt = _capture_system_prompt(
        monkeypatch,
        agent,
        generation,
        model_tool_names=("mcp__browser__browser_navigate",),
    )

    assert "Available tool names: mcp__browser__browser_navigate." in system_prompt
    assert "Never call or invent a tool that is not in this list." in system_prompt


def test_minimax_streaming_requests_token_usage() -> None:
    model = to_langchain_model(
        OpenAICompatibleModel(
            model="MiniMax-M3",
            api_key="test-key",
            base_url="https://api.minimaxi.com/v1",
            provider_id="minimax",
        )
    )

    assert model.stream_usage is True


def test_openai_compatible_model_sets_bounded_request_timeout() -> None:
    """Removing the provider deadline would let a Run remain active forever."""

    model = to_langchain_model(
        OpenAICompatibleModel(
            model="slow-provider",
            api_key="test-key",
            base_url="https://example.test/v1",
            provider_id="custom-provider",
            request_timeout_seconds=12.5,
        )
    )

    assert model.request_timeout == 12.5


def test_openai_compatible_model_preserves_reasoning_content() -> None:
    model = to_langchain_model(
        OpenAICompatibleModel(
            model="deepseek-v4-flash",
            api_key="test-key",
            base_url="https://example.test/v1",
            provider_id="custom-provider",
        )
    )

    generation = model._convert_chunk_to_generation_chunk(
        {
            "choices": [
                {
                    "delta": {
                        "content": None,
                        "reasoning_content": "The",
                        "role": "assistant",
                    },
                    "finish_reason": None,
                }
            ]
        },
        AIMessageChunk,
        None,
    )

    assert generation is not None
    assert generation.message.additional_kwargs["reasoning_content"] == "The"


def test_deepseek_tool_requests_retain_prior_reasoning() -> None:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    model = to_langchain_model(OpenAICompatibleModel(
        model="deepseek-v4-flash", api_key="test-key", base_url="https://example.test/v1"))
    messages = [HumanMessage(content="create chess"), AIMessage(
        content="", additional_kwargs={"reasoning_content": "inspect the project"},
        tool_calls=[{"id": "call", "name": "terminal", "args": {"command": "pwd"}}]),
        ToolMessage(content="/project", tool_call_id="call")]
    payload = model._get_request_payload(messages, tools=[{
        "type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}])
    assert payload["messages"][1]["reasoning_content"] == "inspect the project"
    assert payload["messages"][1]["tool_calls"][0]["id"] == "call"


def test_provider_payload_answers_tool_calls_langchain_rejected() -> None:
    """A malformed MiniMax tool call must not leave the history unanswered.

    MiniMax answers HTTP 400 ``invalid params`` when an assistant message
    declares a tool call that no later tool message answers, so the payload
    boundary has to close the gap LangChain leaves in ``invalid_tool_calls``.
    """

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from bianbu_agent.engines.tool_pairing import INVALID_ARGUMENTS_RESULT

    model = to_langchain_model(OpenAICompatibleModel(
        model="MiniMax-M3", api_key="test-key",
        base_url="https://api.minimaxi.com/v1", provider_id="minimax"))
    messages = [
        HumanMessage(content="列出应用"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call-ok", "name": "list_apps", "args": {}}],
            invalid_tool_calls=[
                {"id": "call-bad", "name": "get_system_info", "args": '["all"]',
                 "error": None}
            ],
        ),
        ToolMessage(content="ok", tool_call_id="call-ok"),
    ]

    payload = model._get_request_payload(messages)
    wire = payload["messages"]

    assert [call["id"] for call in wire[1]["tool_calls"]] == ["call-ok", "call-bad"]
    assert wire[2]["tool_call_id"] == "call-ok"
    assert wire[3]["tool_call_id"] == "call-bad"
    assert wire[3]["content"] == INVALID_ARGUMENTS_RESULT


@pytest.mark.parametrize("model_name,with_tools,expected", [
    ("deepseek-v4-flash", True, True),
    ("deepseek-v4-flash", False, False),
    ("other-model", True, False),
])
def test_reasoning_request_scope(model_name, with_tools, expected) -> None:
    from langchain_core.messages import AIMessage

    model = to_langchain_model(OpenAICompatibleModel(
        model=model_name, api_key="test-key", base_url="https://example.test/v1"))
    messages = [AIMessage(content="previous answer", additional_kwargs={"reasoning_content": "plan"})]
    kwargs = {"tools": [{"type": "function", "function": {"name": "terminal"}}]} if with_tools else {}
    payload = model._get_request_payload(messages, **kwargs)
    assert ("reasoning_content" in payload["messages"][0]) is expected


def test_nonstreaming_deepseek_preserves_reasoning_for_child_tools() -> None:
    model = to_langchain_model(OpenAICompatibleModel(
        model="deepseek-v4-flash", api_key="test-key", base_url="https://example.test/v1"))
    result = model._create_chat_result({
        "id": "reply", "model": "deepseek-v4-flash", "object": "chat.completion",
        "created": 1, "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "answer", "reasoning_content": "plan"}}]})
    assert result.generations[0].message.additional_kwargs["reasoning_content"] == "plan"


def test_default_engine_scans_skills_with_security_agent_namespace(monkeypatch):
    agent = AgentSpec(
        id="bianbu-agent-default",
        name="default",
        skills=("rhythm",),
    )
    generation = SimpleNamespace(
        tools={},
        skills={
            "rhythm": SimpleNamespace(
                spec=SimpleNamespace(
                    display_name="Rhythm",
                    description="Keep rhythm.",
                    instructions="Keep time.",
                )
            )
        },
        policy=object(),
        audit=object(),
    )
    seen: dict[str, object] = {}

    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.delenv("BIANBU_WORKER_AGENT_ID", raising=False)
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(
        "bianbu_agent.security.security_skill_dlp.scan_skill_sections",
        lambda agent_id, sections: seen.update(agent_id=agent_id, sections=sections)
        or sections,
    )
    _capture_system_prompt(monkeypatch, agent, generation)

    assert seen["agent_id"] == "com.bianbu.agent"


def test_default_engine_scans_skills_with_managed_worker_security_namespace(
    monkeypatch,
):
    agent = AgentSpec(
        id="bianbu-agent-default",
        name="default",
        skills=("rhythm",),
    )
    generation = SimpleNamespace(
        tools={},
        skills={
            "rhythm": SimpleNamespace(
                spec=SimpleNamespace(
                    display_name="Rhythm",
                    description="Keep rhythm.",
                    instructions="Keep time.",
                )
            )
        },
        policy=object(),
        audit=object(),
    )
    seen: dict[str, object] = {}

    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.setenv("BIANBU_WORKER_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setenv("BIANBU_AGENT_ID", "local-fallback")
    monkeypatch.setattr(
        "bianbu_agent.security.security_skill_dlp.scan_skill_sections",
        lambda agent_id, sections: seen.update(agent_id=agent_id, sections=sections)
        or sections,
    )
    _capture_system_prompt(monkeypatch, agent, generation)

    assert seen["agent_id"] == "com.bianbu.agent"


@pytest.mark.asyncio
async def test_default_engine_carries_resolved_provider_identity_into_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    graph = object()
    callback = object()
    monkeypatch.setattr(
        "bianbu_agent.engines.default.build_plugin_tools",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "bianbu_agent.engines.default.to_langchain_model",
        lambda model: model,
    )
    monkeypatch.setattr(
        "bianbu_agent.engines.default.build_langgraph_agent",
        lambda **kwargs: graph,
    )
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.upsert_provider(
        ProviderConfig(
            id="openrouter",
            name="OpenRouter",
            protocol="openai-compatible",
            base_url="https://openrouter.ai/api/v1",
            api_key="secret",
        )
    )
    resolved = YAMLModelResolver(tmp_path / "models").resolve_agent_model(
        AgentModel(provider="openrouter", name="qwen")
    )

    engine = build_default_engine(
        AgentSpec(id="resolved-agent", name="Resolved"),
        SimpleNamespace(tools={}, skills={}),
        resolved,
        capture_callback=callback,
    )

    assert engine._provider_id == "openrouter"
    assert engine._capture_callback is callback


@pytest.mark.parametrize("name,provider", [("deepseek-flash", "custom:chat"), ("MiniMax-M3", "minimax")])
def test_agent_profile_contains_resolved_model_without_credentials(monkeypatch, name, provider):
    agent = AgentSpec(id="model-aware", name="Model aware", model=AgentModel(provider="stale", name="old-model"))
    model = OpenAICompatibleModel(model=name, provider_id=provider, api_key="secret-test-key",
                                  base_url="https://private-endpoint.invalid/v1")
    prompt = _capture_system_prompt(monkeypatch, agent, SimpleNamespace(tools={}, skills={}, hooks={}), model=model)
    profile = prompt.split("## Tool Policy")[0]
    assert f'Model: {name}' in profile
    assert f'Model provider ID: {provider}' in profile
    assert "old-model" not in profile
    assert "secret-test-key" not in prompt
    assert "private-endpoint" not in prompt


def test_agent_profile_does_not_invent_unknown_model_identity(monkeypatch):
    prompt = _capture_system_prompt(monkeypatch, AgentSpec(id="unknown", name="Unknown"),
                                   SimpleNamespace(tools={}, skills={}, hooks={}))
    assert "Model: unknown" in prompt
    assert "Model provider ID: unknown" in prompt
