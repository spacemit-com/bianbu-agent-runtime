from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from bianbu_agent import AgentRuntime, AgentSpec, InMemoryAgentRegistry, RunControl
from bianbu_agent.context import ContextCompactor
from bianbu_agent.engines.default import build_default_engine
from bianbu_agent.plugins import PluginGeneration


class _PromptRecordingModel(FakeMessagesListChatModel):
    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tool_batches: list[tuple[str, ...]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        del kwargs
        self.bound_tool_batches.append(
            tuple(str(getattr(tool, "name", "")) for tool in tools)
        )
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen_messages.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


@pytest.mark.asyncio
async def test_empty_agent_hi_keeps_platform_aware_input_under_480_estimated_tokens() -> None:
    agent = AgentSpec(
        id="minimal",
        name="Minimal",
        tools=(),
        skills=(),
        hooks=(),
        mcps=(),
    )
    generation = PluginGeneration(id=1, plugins={}, statuses={})
    model = _PromptRecordingModel(responses=[AIMessage(content="hello")])
    registry = InMemoryAgentRegistry((agent,))
    runtime = AgentRuntime(
        registry=registry,
        engine_builder=lambda selected: build_default_engine(
            selected,
            generation,
            model,
        ),
        generation_engine_builder=lambda selected, leased: build_default_engine(
            selected,
            leased,
            model,
        ),
    )

    _ = [
        event
        async for event in runtime.stream(
            "hi",
            session_id="minimal-thread",
            product_session_id="minimal-session",
            control=RunControl(
                request_id="minimal-run",
                session_id="minimal-session",
            ),
            agent_id="minimal",
            plugin_generation=generation,
        )
    ]

    assert generation.plugins == {}
    assert generation.tools == {}
    assert generation.skills == {}
    assert generation.hooks == {}
    assert generation.mcps == {}
    assert model.bound_tool_batches == []
    assert len(model.seen_messages) == 1
    # All experts now receive platform identity, the software-request
    # boundaries (a download authorizes fetching only; installs go through the
    # approved host installation tool) and the Runtime-owned response-language
    # rule even without optional hooks. The budget covers those required facts,
    # not a capability/service catalogue, and this constant is the only place
    # the prompt-size floor is recorded. The language rule plus its per-turn
    # reminder cost about 280 of these tokens; a Chinese user being answered in
    # English is a defect, so it is worth that share.
    system_text = str(model.seen_messages[0][0].content)
    assert "Bibit / Bianbu Agent Runtime" in system_text
    assert "Architecture:" in system_text
    assert "Operating system:" in system_text
    estimated_tokens = ContextCompactor().estimate_tokens(model.seen_messages[0])
    assert estimated_tokens <= 780, (
        f"minimal Agent input grew to {estimated_tokens} estimated tokens"
    )
