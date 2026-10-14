from dataclasses import dataclass
import asyncio
import json
from pathlib import Path

import pytest

from bianbu_agent import (
    AgentPackageManager,
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    PluginManager,
    PluginRegistration,
    PluginSpec,
    RunEvent,
    RuntimeConfig,
    RuntimeStartupError,
    AgentHome,
    ToolCallContext,
)
from bianbu_agent.inventory import InventorySnapshot
from bianbu_agent.plugins import (
    PluginManager as PackagePluginManager,
    PluginRegistration as PackagePluginRegistration,
    PluginSpec as PackagePluginSpec,
)


@dataclass
class Model:
    model: str


class Engine:
    async def stream(self, prompt, **kwargs):
        yield RunEvent("token", {"text": "ok"})
        yield RunEvent("done", {})


def test_plugin_public_facades_share_the_canonical_classes() -> None:
    assert PluginManager is PackagePluginManager
    assert PluginRegistration is PackagePluginRegistration
    assert PluginSpec is PackagePluginSpec
    assert hasattr(PluginManager, "include_generation_revision")
    assert hasattr(PluginManager, "include_generation_participant")


@pytest.mark.asyncio
async def test_public_sdk_one_file_usage_close_and_reopen(tmp_path: Path) -> None:
    config = RuntimeConfig(data_dir=tmp_path / "sdk")
    default = AgentSpec(id="default", name="Default")
    home = AgentHome(config.data_dir).prepare()
    AgentPackageManager(home, protected_ids={default.id}).create(default)
    sdk = BianbuAgentRuntime(
        config=config,
        default_model=Model("default"),
        agent_id=default.id,
        engine_builder=lambda agent, generation, model: Engine(),
    )

    assert sdk.skills.list() == ()
    assert sdk.plugins.list() == ()
    # Hooks are Plugin members: a Runtime no longer exposes a Hook manager.
    assert not hasattr(sdk, "hooks")

    async with sdk:
        assert (config.data_dir / "models").is_dir()
        assert not (config.data_dir / "skills").exists()
        created = await sdk.agents.create(AgentSpec(id="reviewer", name="Reviewer"))
        assert (config.data_dir / "agents" / "reviewer" / "agent.yaml").is_file()
        session = await sdk.sessions.create("stable", agent_id=created.id)
        result = await (await session.run("hello")).wait()
        assert (
            config.data_dir / "sessions" / "stable" / "session.jsonl.zstd"
        ).is_file()
        # Identity lives beside the log, in the Session's own projection.
        projection = json.loads(
            (config.data_dir / "sessions" / "stable" / "projection.json").read_text(
                encoding="utf-8"
            )
        )
        assert projection["record"]["id"] == "stable"
        assert projection["record"]["agent_id"] == "reviewer"
        assert not (config.data_dir / "runs").exists()
        diagnostics = await sdk.diagnostics()
        assert result.content == "ok"
        assert diagnostics.started is True
        assert diagnostics.generation_id == 1
        assert not hasattr(sdk, "memory")
        assert not hasattr(sdk, "security")
        assert not hasattr(sdk, "audit")

    await sdk.close()
    rebuilt: list[AgentSpec] = []
    reopened = BianbuAgentRuntime(
        config=config,
        default_model=Model("default"),
        agent_id=default.id,
        engine_builder=lambda agent, generation, model: (
            rebuilt.append(agent) or Engine()
        ),
    )
    await reopened.start()
    assert (await reopened.agents.get("reviewer")).name == "Reviewer"
    assert (await reopened.sessions.get("stable")).id == "stable"
    restarted_session = await reopened.sessions.create(
        "after-restart", agent_id="reviewer"
    )
    restarted_result = await (await restarted_session.run("again")).wait()
    assert restarted_result.status == "completed"
    assert rebuilt[-1].id == "reviewer"
    assert rebuilt[-1].name == "Reviewer"
    await reopened.close()


@pytest.mark.asyncio
async def test_injected_plugin_manager_refreshes_after_model_commit(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(data_dir=tmp_path / "sdk")
    manager = PluginManager(
        snapshot_provider=lambda: InventorySnapshot(revision=0, items=()),
        workspace_root=config.data_dir / "workspace",
        plugin_data_root=config.data_dir / "workspace" / "plugin-data",
    )
    await manager.start()
    default = AgentSpec(id="default", name="Default")
    sdk = BianbuAgentRuntime(
        config=config,
        default_model=Model("default"),
        agent_id=default.id,
        agent_registry=InMemoryAgentRegistry((default,)),
        plugin_manager=manager,
        engine_builder=lambda *args: Engine(),
    )

    await sdk.start()
    try:
        initial_generation = manager.current_generation
        await sdk.model_configs.create_project_capability(
            capability="vision",
            model="fixture-vision",
            base_url="https://vision.example.test/v1",
            api_key="fixture-vision-secret",
        )

        assert manager.current_generation.id > initial_generation.id
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_public_sdk_rejects_invalid_default_agent_and_cleans_up(
    tmp_path: Path,
) -> None:
    default = AgentSpec(id="default", name="Default", tools=("missing",))
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / "sdk"),
        default_model=Model("default"),
        agent_id=default.id,
        agent_registry=InMemoryAgentRegistry((default,)),
        engine_builder=lambda agent, generation, model: Engine(),
    )

    with pytest.raises(RuntimeStartupError, match="missing tools"):
        await sdk.start()

    assert (await sdk.diagnostics()).started is False
    await sdk.close()


def test_public_sdk_rejects_plugin_services_with_custom_manager(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="plugin_services/managed_plugin_services cannot be combined",
    ):
        BianbuAgentRuntime(
            config=RuntimeConfig(data_dir=tmp_path / "sdk"),
            default_model=Model("default"),
            agent_id="default",
            agent_registry=InMemoryAgentRegistry(
                (AgentSpec(id="default", name="Default"),)
            ),
            plugin_manager=object(),
            plugin_services={"runtime_services": "injected"},
            engine_builder=lambda agent, generation, model: Engine(),
        )


def test_public_sdk_allows_empty_plugin_services_with_custom_manager(
    tmp_path: Path,
) -> None:
    manager = object()

    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / "sdk"),
        default_model=Model("default"),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="default", name="Default"),)
        ),
        plugin_manager=manager,
        plugin_services={},
        engine_builder=lambda agent, generation, model: Engine(),
    )

    assert sdk.plugin_runtime is manager


@pytest.mark.asyncio
async def test_default_plugin_service_lifecycle_wraps_sdk_runtime(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []

    class Service:
        async def start(self, sdk, generation) -> None:
            calls.append(("start", generation.id))
            assert sdk.runtime is sdk.runs

        async def close(self) -> None:
            calls.append(("close", None))

    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / "sdk"),
        default_model=Model("default"),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="default", name="Default"),)
        ),
        managed_plugin_services={"service": Service()},
        engine_builder=lambda agent, generation, model: Engine(),
    )

    await sdk.start()
    await sdk.close()
    await sdk.close()

    assert calls == [("start", 1), ("close", None)]


@pytest.mark.asyncio
async def test_plain_plugin_service_is_not_a_lifecycle_participant(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class PlainService:
        def start(self) -> None:
            calls.append("start")

        def close(self) -> None:
            calls.append("close")

    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / "sdk"),
        default_model=Model("default"),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="default", name="Default"),)
        ),
        plugin_services={"plain": PlainService()},
        engine_builder=lambda agent, generation, model: Engine(),
    )

    await sdk.start()
    await sdk.close()

    assert calls == []


@pytest.mark.asyncio
async def test_plugin_service_close_failure_does_not_skip_sdk_cleanup(
    tmp_path: Path,
) -> None:
    class FailingService:
        attempts = 0

        async def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("close failed")

    service = FailingService()
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / "sdk"),
        default_model=Model("default"),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="default", name="Default"),)
        ),
        managed_plugin_services={"failing": service},
        engine_builder=lambda agent, generation, model: Engine(),
    )
    await sdk.start()

    with pytest.raises(ExceptionGroup, match="Runtime close failed"):
        await sdk.close()

    assert sdk._opened == []
    with pytest.raises(RuntimeError, match="PluginManager is not started"):
        _ = sdk.plugin_runtime.current_generation

    await sdk.close()
    assert service.attempts == 2


@pytest.mark.asyncio
async def test_plugin_service_close_preserves_cancellation_after_cleanup(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class CancellingService:
        attempts = 0

        async def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise asyncio.CancelledError

    class OtherService:
        async def close(self) -> None:
            calls.append("other-closed")

    cancelling = CancellingService()
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / "sdk"),
        default_model=Model("default"),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="default", name="Default"),)
        ),
        managed_plugin_services={
            "cancelling": cancelling,
            "other": OtherService(),
        },
        engine_builder=lambda agent, generation, model: Engine(),
    )
    await sdk.start()

    with pytest.raises(asyncio.CancelledError):
        await sdk.close()

    assert calls == ["other-closed"]
    assert sdk._opened == []
    with pytest.raises(RuntimeError, match="PluginManager is not started"):
        _ = sdk.plugin_runtime.current_generation

    await sdk.close()
    assert cancelling.attempts == 2
