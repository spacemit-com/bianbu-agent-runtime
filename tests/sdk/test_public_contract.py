from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from bianbu_agent import (
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    PluginRegistration,
    PluginSpec,
    RunEvent,
    RuntimeConfig,
)
from bianbu_agent.plugins import PluginHealth, PluginManifest


@dataclass
class Model:
    model: str


class Engine:
    async def stream(self, prompt, **kwargs):
        yield RunEvent("done", {"content": "ok"})


class FakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _agent_options() -> dict[str, object]:
    agent = AgentSpec(id="default", name="Default")
    return {
        "agent_id": agent.id,
        "agent_registry": InMemoryAgentRegistry((agent,)),
    }


@pytest.mark.asyncio
async def test_public_constructor_accepts_plugin_specs_and_registrations(
    tmp_path: Path,
) -> None:
    manifest = PluginManifest(
        id="example.lifecycle",
        version="1.0.0",
        config_schema={"type": "object"},
    )

    class Plugin:
        async def setup(self, context):
            return None

        async def start(self):
            return None

        async def health(self):
            return PluginHealth.healthy()

        async def reconfigure(self, config):
            return None

        async def stop(self):
            return None

    Plugin.manifest = manifest
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(
            data_dir=tmp_path / "sdk",
        ),
        default_model=Model("default"),
        **_agent_options(),
        plugins=(PluginSpec(id=manifest.id, config={"enabled": True}),),
        plugin_registrations=(
            PluginRegistration(
                manifest=manifest,
                factory=lambda context: Plugin(),
            ),
        ),
        engine_builder=lambda agent, generation, model: Engine(),
    )

    async with sdk:
        assert manifest.id in sdk.plugin_runtime.current_generation.plugins


@pytest.mark.asyncio
async def test_session_public_contract_and_run_handle_properties(
    tmp_path: Path,
) -> None:
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(
            data_dir=tmp_path / "sdk",
        ),
        default_model=Model("default"),
        **_agent_options(),
        engine_builder=lambda agent, generation, model: Engine(),
    )

    async with sdk:
        assert sdk.runtime is sdk.runs
        diagnostics = await sdk.diagnostics()
        assert diagnostics.waiting_runs == 0
        assert await sdk.plugin_runtime.health() == {}
        generation = sdk.plugin_runtime.current_generation
        assert not hasattr(generation, "memory")
        assert not hasattr(generation, "policy")
        assert not hasattr(generation, "audit")
        created = await sdk.sessions.get_or_create(
            session_id="stable",
            agent_id="default",
            metadata={"source": "test"},
        )
        restored = await sdk.sessions.get_or_create(session_id="stable", agent_id="default")
        assert restored.id == created.id
        restarted = await created.restart()
        assert restarted.id == "stable"

        run = await restarted.run(
            "hello",
            metadata={"source": "test"},
        )
        assert run.id == run.request_id
        assert run.session_id == "stable"
        assert run.agent_id == "default"
        assert run.generation_id == sdk.plugin_runtime.current_generation.id
        result = await run.wait()
        restored_run = await sdk.runs.get(run.id)
        assert restored_run is not None
        assert restored_run.id == run.id
        assert (await restored_run.wait()) == result


@pytest.mark.asyncio
async def test_default_engine_and_checkpointer_run_without_framework_injection(
    tmp_path: Path,
) -> None:
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / "sdk"),
        default_model=FakeModel(responses=[AIMessage(content="default works")]),
        **_agent_options(),
    )

    async with sdk:
        session = await sdk.sessions.create("one-file", agent_id="default")
        result = await (await session.run("hello")).wait()

    assert result.status == "completed"
    assert result.content == "default works"


@pytest.mark.asyncio
async def test_default_engine_path_folds_the_session_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured = []

    def fake_default_engine(
        agent,
        generation,
        model,
        *,
        transcript_provider=None,
        capture_callback=None,
        context_settings=None,
    ):
        captured.append((transcript_provider, capture_callback))
        assert callable(transcript_provider)
        assert context_settings is not None
        return Engine()

    monkeypatch.setattr(
        "bianbu_agent.server.runtime.build_default_engine", fake_default_engine
    )
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / "sdk"),
        default_model=Model("default"),
        **_agent_options(),
    )
    async with sdk:
        session = await sdk.sessions.create("session", agent_id="default")
        result = await (await session.run("hello")).wait()
        assert result.status == "completed"
        assert sdk.provider_capture_callback is captured[0][1]
        assert (
            tmp_path / "sdk" / "sessions" / "session" / "session.jsonl.zstd"
        ).is_file()
