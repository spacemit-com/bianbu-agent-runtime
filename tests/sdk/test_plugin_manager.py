from dataclasses import replace
from pathlib import Path
import json
import sys

import pytest
import yaml

from bianbu_agent.mcp.client import MCPClient
from bianbu_agent.mcp.sources import runtime_mcp_id

from bianbu_agent.inventory import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InstalledCapability,
    InventorySnapshot,
)
from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent.plugins import (
    CapabilityReloadError as DirectoryCapabilityReloadError,
    PluginManager as DirectoryPluginManager,
)
from bianbu_agent.plugins import (
    CapabilityReloadError,
    PluginManager,
    PluginCandidate,
    PluginHealth,
    PluginManifest,
    PluginState,
    PluginSpec,
    ToolCallContext,
)


def _write_plugin(root: Path) -> None:
    skill = root / "skills" / "observer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: observer\ndescription: Observe generations\n---\nObserve.",
        encoding="utf-8",
    )
    (root / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": root.name,
                "version": "1.0.0",
                "description": "Observer plugin fixture.",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_skill(root: Path, name: str) -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use {name}\n"
        f"version: 1.0.0\n---\nUse {name}."
    )


def _source(
    kind: CapabilityKind,
    capability_id: str,
    root: Path,
    manifest: str | None,
):
    files = tuple(
        path
        for path in regular_files(root)
        if path.relative_to(root).parts[:1] != ("data",)
    )
    return CapabilitySource(
        kind=kind,
        capability_id=capability_id,
        version="1.0.0",
        root=root,
        manifest_path=(
            root / "plugin.yaml"
            if kind is CapabilityKind.PLUGIN
            else root if manifest is None else root / manifest
        ),
        content_digest=digest_directory(root, files),
        origin=CapabilityOrigin.STANDALONE,
    )


class _Snapshots:
    def __init__(self, snapshot: InventorySnapshot) -> None:
        self.current = snapshot

    async def __call__(self) -> InventorySnapshot:
        return self.current


class _GenerationParticipant:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log
        self.state: int | None = None
        self.pending = False
        self.fail_prepare = False
        self.fail_activate = False
        self.fail_abort = False
        self.fail_rollback = False

    def begin_generation_candidate(self) -> None:
        if self.pending:
            raise RuntimeError(f"{self.name} candidate leaked")
        self.pending = True
        self.log.append(f"{self.name}.begin")

    def prepare_generation_candidate(self, generation) -> tuple[int | None, int]:
        self.log.append(f"{self.name}.prepare")
        if self.fail_prepare:
            raise RuntimeError(f"{self.name} prepare failed")
        return self.state, generation.id

    def activate_generation_candidate(self, token: tuple[int | None, int]) -> None:
        self.log.append(f"{self.name}.activate")
        self.pending = False
        self.state = token[1]
        if self.fail_activate:
            raise RuntimeError(f"{self.name} activate failed")

    def rollback_generation_candidate(self, token: tuple[int | None, int]) -> None:
        self.log.append(f"{self.name}.rollback")
        self.pending = False
        self.state = token[0]
        if self.fail_rollback:
            raise RuntimeError(f"{self.name} rollback failed synthetic-secret")

    def abort_generation_candidate(self) -> None:
        self.log.append(f"{self.name}.abort")
        self.pending = False
        if self.fail_abort:
            raise RuntimeError(f"{self.name} abort failed")


@pytest.mark.asyncio
async def test_one_failed_mcp_does_not_reject_the_other_mcp_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = {}
    for mcp_id, command in (("mcp.good", "good"), ("mcp.broken", "broken")):
        root = tmp_path / mcp_id
        root.mkdir()
        (root / "mcp.json").write_text(
            json.dumps({"transport": "stdio", "command": command}),
            encoding="utf-8",
        )
        roots[mcp_id] = root

    async def list_tools(client: MCPClient):
        if client.config.command == "broken":
            raise ConnectionError("unavailable")
        return ()

    async def start(_client):
        pass

    monkeypatch.setattr(MCPClient, "start", start)
    monkeypatch.setattr(MCPClient, "list_tools", list_tools)
    snapshot = InventorySnapshot(
        revision=1,
        items=tuple(
            InstalledCapability(
                source=_source(CapabilityKind.MCP, mcp_id, root, "mcp.json"),
                enabled=True,
            )
            for mcp_id, root in roots.items()
        ),
    )
    manager = PluginManager(
        snapshot_provider=lambda: snapshot,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )

    generation = await manager.start()
    try:
        assert set(generation.mcps) == {"mcp.good"}
        broken = generation.statuses[runtime_mcp_id("mcp.broken")]
        assert broken.state.value == "failed"
        assert broken.diagnostics[0].code == "plugin_start_failed"
        # Worker startup publishes Runtime diagnostics before accepting runs.
        # Failed MCPs have statuses but deliberately have no running instance.
        from types import SimpleNamespace
        from bianbu_agent.server.runtime import BianbuAgentRuntime
        diagnostics = await BianbuAgentRuntime.diagnostics(SimpleNamespace(
            _started=True, _closing=False, _checkpointer=None,
            plugin_runtime=SimpleNamespace(current_generation=generation),
            runs=SimpleNamespace(active_count=0, waiting_count=0),
            _recent_errors=[], _security_pending={},
        ))
        row = next(row for row in diagnostics.plugin_statuses
                   if row[0] == runtime_mcp_id("mcp.broken"))
        assert row[1:] == ("failed", "unhealthy", ())
        from bianbu_agent import AgentModel, AgentSpec, InMemoryAgentRegistry
        from bianbu_agent.agents.manager import AgentManager
        spec = AgentSpec(
            id="default",
            name="Default",
            model=AgentModel("test", "test"),
            mcps=("mcp.good", "mcp.broken"),
        )
        agents = AgentManager(store=InMemoryAgentRegistry((spec,)),
            model_registry=SimpleNamespace(resolve=lambda model: model),
            generation_provider=lambda: generation)
        selected = await agents.materialize("default", generation)
        # The unavailable server keeps its failed status but never a generation
        # entry, so the expert materializes with the healthy selection only.
        assert selected.mcps == ("mcp.good",)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_plugin_manager_keeps_a_preparing_plugin_running(
    tmp_path: Path,
) -> None:
    """A Plugin that is still starting is not a Plugin that failed.

    An MCP server that is downloading its package reports ``preparing``: the
    host must keep it running, because stopping it would cancel the very work
    that is going to make it healthy.
    """

    manifest = PluginManifest(
        id="host.preparing",
        version="1.0.0",
    )

    class PreparingPlugin:
        def __init__(self) -> None:
            self.stopped = False

        async def setup(self, context):
            return None

        async def start(self):
            return None

        async def health(self):
            return PluginHealth(status="preparing", message="still starting")

        async def reconfigure(self, config):
            return None

        async def stop(self):
            self.stopped = True

    PreparingPlugin.manifest = manifest
    instance = PreparingPlugin()

    manager = PluginManager(
        snapshot_provider=lambda: InventorySnapshot(revision=0),
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugins",
        explicit_candidates=(
            PluginCandidate(
                factory=lambda _context: instance,
                manifest=manifest,
            ),
        ),
    )

    generation = await manager.start()
    try:
        assert set(generation.plugins) == {"host.preparing"}
        status = generation.statuses["host.preparing"]
        assert status.health.status == "preparing"
        assert status.state is PluginState.RUNNING
        assert status.diagnostics == ()
        assert instance.stopped is False
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_plugin_manager_always_enables_explicit_host_plugins(
    tmp_path: Path,
) -> None:
    manifest = PluginManifest(
        id="host.required",
        version="1.0.0",
    )

    class HostPlugin:
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

    HostPlugin.manifest = manifest

    manager = PluginManager(
        snapshot_provider=lambda: InventorySnapshot(revision=0),
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugins",
        explicit_candidates=(
            PluginCandidate(factory=lambda context: HostPlugin(), manifest=manifest),
        ),
    )

    generation = await manager.start()

    assert set(generation.plugins) == {"host.required"}
    await manager.close()


@pytest.mark.asyncio
async def test_plugin_manager_uses_shared_effective_config_composition(
    tmp_path: Path,
) -> None:
    manifest = PluginManifest(
        id="host.configured",
        version="1.0.0",
        config_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "default": "default"},
                "limit": {"type": "integer", "default": 1},
            },
            "additionalProperties": False,
        },
    )

    class ConfiguredPlugin:
        def __init__(self) -> None:
            self.manifest = manifest

        async def setup(self, context):
            self.context = context

        async def start(self):
            return None

        async def health(self):
            return PluginHealth.healthy()

        async def reconfigure(self, config):
            return None

        async def stop(self):
            return None

    (tmp_path / "plugins.yaml").write_text(
        """\
schema_version: 1
plugins:
  host.configured:
    config:
      mode: home
""",
        encoding="utf-8",
    )
    manager = PluginManager(
        snapshot_provider=lambda: InventorySnapshot(revision=0),
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
        explicit_candidates=(
            PluginCandidate(factory=lambda _context: ConfiguredPlugin(), manifest=manifest),
        ),
        plugin_specs=(PluginSpec(id=manifest.id, config={"limit": 9}),),
        runtime_plugin_configs={manifest.id: {"mode": "runtime", "limit": 5}},
        home_overlay_path=tmp_path / "plugins.yaml",
    )

    generation = await manager.start()

    runtime = generation.plugins[manifest.id]
    assert dict(runtime.config) == {"mode": "home", "limit": 9}
    assert dict(runtime.config_provenance) == {
        "limit": "programmatic",
        "mode": "home",
    }
    await manager.close()


@pytest.mark.asyncio
async def test_plugin_manager_reports_enabled_missing_plugin(tmp_path: Path) -> None:
    (tmp_path / "plugins.yaml").write_text(
        """\
schema_version: 1
plugins:
  vendor.missing:
    enabled: true
""",
        encoding="utf-8",
    )
    manager = PluginManager(
        snapshot_provider=lambda: InventorySnapshot(revision=0),
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
        home_overlay_path=tmp_path / "plugins.yaml",
    )

    with pytest.raises(CapabilityReloadError) as failure:
        await manager.start()

    assert failure.value.diagnostics[0].code == "plugin_not_found"
    assert failure.value.diagnostics[0].plugin_id == "vendor.missing"


@pytest.mark.asyncio
async def test_plugin_manager_enables_first_class_mcp_candidates(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mcps" / "mcp.echo"
    root.mkdir(parents=True)
    server = Path(__file__).parent / "mcp" / "fixtures" / "stdio_server.py"
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "display_name": "Echo Server",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(server)],
            }
        ),
        encoding="utf-8",
    )
    source = _source(CapabilityKind.MCP, "mcp.echo", root, "mcp.json")
    snapshot = InventorySnapshot(
        revision=1,
        items=(InstalledCapability(source=source, enabled=True),),
    )
    manager = DirectoryPluginManager(
        snapshot_provider=lambda: snapshot,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )

    generation = await manager.start()

    assert set(generation.mcps) == {"mcp.echo"}
    assert generation.mcps["mcp.echo"].display_name == "Echo Server"
    assert generation.mcp_tools["mcp.echo"] == ("mcp.echo/echo",)
    await manager.close()


@pytest.mark.asyncio
async def test_plugin_manager_reload_publishes_new_generation_and_retires_old(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "plugins" / "vendor.observer"
    _write_plugin(plugin_root)
    first = InventorySnapshot(
        revision=1,
        items=(
            InstalledCapability(
                source=_source(
                    CapabilityKind.PLUGIN,
                    "vendor.observer",
                    plugin_root,
                    None,
                ),
                enabled=True,
            ),
        ),
    )
    snapshots = _Snapshots(first)
    manager = PluginManager(
        snapshot_provider=snapshots,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    await manager.start()

    assert manager.current_generation.inventory_revision == 1

    async with manager.lease() as old_generation:
        old_plugin = old_generation.plugins["vendor.observer"].instance
        snapshots.current = InventorySnapshot(revision=2)
        result = await manager.reload_installed()

        assert result.previous_generation_id == old_generation.id
        assert result.generation_id != old_generation.id
        assert result.inventory_revision == 2
        assert manager.current_generation.id == result.generation_id
        assert manager.current_generation.inventory_revision == 2
        assert "vendor.observer" not in manager.current_generation.plugins
        assert old_plugin.stopped is False

    assert old_plugin.stopped is True
    await manager.close()


@pytest.mark.asyncio
async def test_forced_reload_does_not_publish_an_equivalent_generation(tmp_path: Path) -> None:
    """An explicit probe avoids generation churn when MCP tools are unchanged."""

    snapshot = InventorySnapshot(revision=1)
    manager = PluginManager(
        snapshot_provider=lambda: snapshot,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    first = await manager.start()

    result = await manager.reload_installed(force=True)

    assert result.changed is False
    assert result.previous_generation_id == first.id
    assert result.generation_id == first.id
    await manager.close()


@pytest.mark.asyncio
async def test_generation_activation_failure_rolls_back_every_participant(
    tmp_path: Path,
) -> None:
    snapshots = _Snapshots(InventorySnapshot(revision=1))
    log: list[str] = []
    first = _GenerationParticipant("first", log)
    second = _GenerationParticipant("second", log)
    manager = DirectoryPluginManager(
        snapshot_provider=snapshots,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    manager.include_generation_participant(first)
    manager.include_generation_participant(second)
    old_generation = await manager.start()
    assert first.state == second.state == old_generation.id

    second.fail_activate = True
    snapshots.current = InventorySnapshot(revision=2)
    with pytest.raises(
        DirectoryCapabilityReloadError, match="second activate failed"
    ):
        await manager.reload_installed()

    assert manager.current_generation is old_generation
    assert first.state == second.state == old_generation.id
    assert log[-4:] == [
        "first.activate",
        "second.activate",
        "second.rollback",
        "first.rollback",
    ]
    assert manager._prepared_generations == {}
    assert manager._generation_candidate_active is False

    second.fail_activate = False
    retried = await manager.reload_installed()
    assert retried.changed is True
    assert first.state == second.state == retried.generation_id
    await manager.close()


@pytest.mark.asyncio
async def test_candidate_abort_is_best_effort_and_preserves_prepare_error(
    tmp_path: Path,
) -> None:
    snapshots = _Snapshots(InventorySnapshot(revision=1))
    log: list[str] = []
    participants = [
        _GenerationParticipant("first", log),
        _GenerationParticipant("second", log),
        _GenerationParticipant("third", log),
    ]
    manager = DirectoryPluginManager(
        snapshot_provider=snapshots,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    for participant in participants:
        manager.include_generation_participant(participant)
    old_generation = await manager.start()

    participants[1].fail_prepare = True
    participants[1].fail_abort = True
    snapshots.current = InventorySnapshot(revision=2)
    with pytest.raises(
        DirectoryCapabilityReloadError, match="second prepare failed"
    ) as failure:
        await manager.reload_installed()

    assert manager.current_generation is old_generation
    assert log[-3:] == ["third.abort", "second.abort", "first.abort"]
    assert all(participant.pending is False for participant in participants)
    assert manager._generation_candidate_active is False
    prepare_error = failure.value.__cause__
    assert prepare_error is not None
    assert "second prepare failed" in str(prepare_error)
    cleanup_error = prepare_error.__cause__
    assert isinstance(cleanup_error, BaseExceptionGroup)
    assert [str(exc) for exc in cleanup_error.exceptions] == [
        "second abort failed"
    ]

    participants[1].fail_prepare = False
    participants[1].fail_abort = False
    retried = await manager.reload_installed()
    assert retried.changed is True
    await manager.close()


@pytest.mark.asyncio
async def test_reconfigure_is_rejected_when_generation_participants_are_present(
    tmp_path: Path,
) -> None:
    participant = _GenerationParticipant("runtime", [])
    manager = DirectoryPluginManager(
        snapshot_provider=lambda: InventorySnapshot(revision=1),
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    manager.include_generation_participant(participant)
    generation = await manager.start()

    with pytest.raises(RuntimeError, match="reload_installed"):
        await manager.reconfigure({})

    assert manager.current_generation is generation
    assert participant.state == generation.id
    await manager.close()


@pytest.mark.asyncio
async def test_reload_rollback_failure_poisons_and_quarantines_manager(
    tmp_path: Path,
) -> None:
    snapshots = _Snapshots(InventorySnapshot(revision=1))
    first = _GenerationParticipant("first", [])
    second = _GenerationParticipant("second", [])
    manager = DirectoryPluginManager(
        snapshot_provider=snapshots,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    manager.include_generation_participant(first)
    manager.include_generation_participant(second)
    await manager.start()

    first.fail_rollback = True
    second.fail_activate = True
    snapshots.current = InventorySnapshot(revision=2)
    with pytest.raises(RuntimeError, match="unavailable") as failure:
        await manager.reload_installed()

    assert "synthetic-secret" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert manager._host is None
    assert manager._prepared_generations == {}
    assert manager._generation_candidate_active is False
    with pytest.raises(RuntimeError, match="unavailable"):
        _ = manager.current_generation
    with pytest.raises(RuntimeError, match="unavailable"):
        async with manager.lease():
            pass
    with pytest.raises(RuntimeError, match="unavailable"):
        await manager.reload_installed()
    with pytest.raises(RuntimeError, match="unavailable"):
        await manager.reconfigure({})
    with pytest.raises(RuntimeError, match="unavailable"):
        await manager.start()
    await manager.close()


@pytest.mark.asyncio
async def test_initial_activation_rollback_failure_leaves_manager_closed_and_poisoned(
    tmp_path: Path,
) -> None:
    first = _GenerationParticipant("first", [])
    second = _GenerationParticipant("second", [])
    first.fail_rollback = True
    second.fail_activate = True
    manager = DirectoryPluginManager(
        snapshot_provider=lambda: InventorySnapshot(revision=1),
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    manager.include_generation_participant(first)
    manager.include_generation_participant(second)

    with pytest.raises(RuntimeError, match="unavailable") as failure:
        await manager.start()

    assert "synthetic-secret" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert manager._host is None
    await manager.close()
    with pytest.raises(RuntimeError, match="unavailable"):
        await manager.start()


@pytest.mark.asyncio
async def test_plugin_manager_rejects_tampering_without_inventory_revision(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "plugins" / "vendor.observer"
    _write_plugin(plugin_root)
    snapshot = InventorySnapshot(
        revision=1,
        items=(
            InstalledCapability(
                source=_source(
                    CapabilityKind.PLUGIN,
                    "vendor.observer",
                    plugin_root,
                    None,
                ),
                enabled=True,
            ),
        ),
    )
    manager = PluginManager(
        snapshot_provider=_Snapshots(snapshot),
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    generation = await manager.start()
    definition = plugin_root / "skills" / "observer" / "SKILL.md"
    definition.write_text(
        definition.read_text(encoding="utf-8") + "\nTampered.\n",
        encoding="utf-8",
    )
    with pytest.raises(CapabilityReloadError, match="verify"):
        await manager.reload_installed()
    assert manager.current_generation.id == generation.id
    await manager.close()


@pytest.mark.asyncio
async def test_plugin_manager_accepts_inventory_object_and_sync_snapshot_provider(
    tmp_path: Path,
) -> None:
    snapshot = InventorySnapshot(revision=7)

    class Inventory:
        def snapshot(self) -> InventorySnapshot:
            return snapshot

    manager = PluginManager(
        inventory=Inventory(),
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
        discover_environment=False,
    )

    generation = await manager.start()

    assert generation.inventory_revision == 7
    await manager.close()


@pytest.mark.asyncio
async def test_plugin_spec_disable_removes_plugin_owned_capabilities(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "plugins" / "vendor.observer"
    _write_plugin(plugin_root)
    _write_skill(plugin_root / "skills" / "nested" / "review", "review")
    mcp_root = plugin_root / "mcps" / "weather"
    mcp_root.mkdir(parents=True)
    (mcp_root / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": "weather-mcp"}),
        encoding="utf-8",
    )
    mcp_source = replace(
        _source(CapabilityKind.MCP, "vendor.observer/weather", mcp_root, "mcp.json"),
        origin=CapabilityOrigin.PLUGIN,
        plugin_id="vendor.observer",
    )
    snapshot = InventorySnapshot(
        revision=1,
        items=(
            InstalledCapability(
                source=_source(
                    CapabilityKind.PLUGIN,
                    "vendor.observer",
                    plugin_root,
                    None,
                ),
                enabled=True,
            ),
            InstalledCapability(source=mcp_source, enabled=True),
        ),
    )
    manager = PluginManager(
        snapshot_provider=lambda: snapshot,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
        plugin_specs=(PluginSpec(id="vendor.observer", enabled=False),),
    )

    generation = await manager.start()

    assert generation.plugins == {}
    assert generation.skills == {}
    assert generation.mcps == {}
    await manager.close()


@pytest.mark.asyncio
async def test_plugin_manager_failed_reload_preserves_active_generation(
    tmp_path: Path,
) -> None:
    first_skill = tmp_path / "skills" / "shared"
    _write_skill(first_skill, "shared")
    snapshots = _Snapshots(
        InventorySnapshot(
            revision=1,
            items=(
                InstalledCapability(
                    source=_source(
                        CapabilityKind.SKILL,
                        "shared",
                        first_skill,
                        "SKILL.md",
                    ),
                    enabled=True,
                ),
            ),
        )
    )
    manager = PluginManager(
        snapshot_provider=snapshots,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    first_generation = await manager.start()
    duplicate = tmp_path / "skills-2" / "shared"
    _write_skill(duplicate, "shared")
    snapshots.current = InventorySnapshot(
        revision=2,
        items=(
            *snapshots.current.items,
            InstalledCapability(
                source=_source(
                    CapabilityKind.SKILL,
                    "second-record",
                    duplicate,
                    "SKILL.md",
                ),
                enabled=True,
            ),
        ),
    )

    with pytest.raises(CapabilityReloadError):
        await manager.reload_installed()

    assert manager.current_generation.id == first_generation.id
    assert set(manager.current_generation.skills) == {"shared"}
    await manager.close()


@pytest.mark.asyncio
async def test_skill_resource_paths_remain_pinned_during_update_and_delete(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills" / "shared"
    _write_skill(root, "shared")
    (root / "reference.txt").write_text("old resource", encoding="utf-8")
    def snapshot(revision: int) -> InventorySnapshot:
        return InventorySnapshot(revision=revision, items=(InstalledCapability(
            source=_source(CapabilityKind.SKILL, "shared", root, "SKILL.md"),
            enabled=True,
        ),))

    snapshots = _Snapshots(snapshot(1))
    manager = PluginManager(
        snapshot_provider=snapshots,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    await manager.start()
    try:
        async with manager.lease() as original:
            old_path = Path(original.skills["shared"].spec.resources["reference.txt"])
            retired = tmp_path / "retired-skill"
            root.rename(retired)
            _write_skill(root, "shared")
            (root / "reference.txt").write_text("new resource", encoding="utf-8")
            snapshots.current = snapshot(2)
            await manager.reload_installed()
            assert old_path.read_text(encoding="utf-8") == "old resource"
            async with manager.lease() as updated:
                new_path = Path(updated.skills["shared"].spec.resources["reference.txt"])
                assert new_path.read_text(encoding="utf-8") == "new resource"
                root.rename(tmp_path / "retired-skill-2")
                snapshots.current = InventorySnapshot(revision=3, items=())
                await manager.reload_installed()
                assert old_path.read_text(encoding="utf-8") == "old resource"
                assert new_path.read_text(encoding="utf-8") == "new resource"
                async with manager.lease() as deleted:
                    assert "shared" not in deleted.skills
            assert not new_path.exists()
            assert old_path.exists()
        assert not old_path.exists()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_mcp_connections_survive_update_and_delete_until_last_lease(tmp_path):
    import os
    import sys
    from types import SimpleNamespace
    script = tmp_path / 'server.py'
    script.write_text('import os,sys\nfrom mcp.server.fastmcp import FastMCP\n'
        'app=FastMCP("lease")\n@app.tool()\ndef version() -> str:\n'
        '    return sys.argv[1]+":"+str(os.getpid())\napp.run(transport="stdio")\n')
    root = tmp_path / 'mcp.version'
    root.mkdir()
    def snapshot(version):
        (root / 'mcp.json').write_text(json.dumps({'transport': 'stdio',
            'command': sys.executable, 'args': [str(script), str(version)]}))
        return InventorySnapshot(revision=version, items=(InstalledCapability(
            source=_source(CapabilityKind.MCP, 'mcp.version', root, 'mcp.json'),
            enabled=True),))
    snapshots = _Snapshots(snapshot(1))
    manager = PluginManager(snapshot_provider=snapshots,
        workspace_root=tmp_path / 'workspace', plugin_data_root=tmp_path / 'plugin-data')
    context = ToolCallContext('loop', 'session', 'default')
    async def read(generation):
        result = await generation.tools['mcp.version/version'].handler(context, {})
        version, pid = json.loads(result.content)['result'].split(':')
        return version, int(pid)
    await manager.start()
    try:
        async with manager.lease() as first:
            before = await read(first)
            assert before[0] == '1'
            snapshots.current = snapshot(2)
            await manager.reload_installed()
            assert await read(first) == before
            async with manager.lease() as second:
                after = await read(second)
                assert after[0] == '2' and after[1] != before[1]
                snapshots.current = InventorySnapshot(revision=3)
                await manager.reload_installed()
                assert not manager.current_generation.mcps
                assert await read(first) == before
                assert await read(second) == after
            with pytest.raises(ProcessLookupError):
                os.kill(after[1], 0)
            os.kill(before[1], 0)
        with pytest.raises(ProcessLookupError):
            os.kill(before[1], 0)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_leasing_adopts_powers_installed_since_the_last_run(tmp_path: Path) -> None:
    """An Agent that installs an MCP or Plugin for itself must get it next run.

    Skills arrive through their own watcher; MCP servers and Plugins only appear
    after the file-backed host is rebuilt, and that rebuild may not interrupt a
    Generation another run still holds.
    """

    plugin_root = tmp_path / "plugins" / "vendor.observer"
    _write_plugin(plugin_root)
    snapshots = _Snapshots(InventorySnapshot(revision=1))
    manager = PluginManager(
        snapshot_provider=snapshots,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    await manager.start()
    assert manager.current_generation.inventory_revision == 1

    snapshots.current = InventorySnapshot(
        revision=2,
        items=(
            InstalledCapability(
                source=_source(
                    CapabilityKind.PLUGIN,
                    "vendor.observer",
                    plugin_root,
                    None,
                ),
                enabled=True,
            ),
        ),
    )

    async with manager.lease() as generation:
        assert "vendor.observer" in generation.plugins

    await manager.close()


@pytest.mark.asyncio
async def test_leasing_does_not_rebuild_the_host_under_an_active_run(tmp_path: Path) -> None:
    """A rebuild may not interrupt a Generation another run still holds."""

    plugin_root = tmp_path / "plugins" / "vendor.observer"
    _write_plugin(plugin_root)
    snapshots = _Snapshots(InventorySnapshot(revision=1))
    manager = PluginManager(
        snapshot_provider=snapshots,
        workspace_root=tmp_path / "workspace",
        plugin_data_root=tmp_path / "plugin-data",
    )
    await manager.start()

    async with manager.lease() as outer:
        assert "vendor.observer" not in outer.plugins
        snapshots.current = InventorySnapshot(
            revision=2,
            items=(
                InstalledCapability(
                    source=_source(
                        CapabilityKind.PLUGIN,
                        "vendor.observer",
                        plugin_root,
                        None,
                    ),
                    enabled=True,
                ),
            ),
        )
        async with manager.lease() as inner:
            assert inner.id == outer.id
        await manager.refresh_capabilities()
        assert manager.current_generation.id == outer.id
        assert "vendor.observer" not in outer.plugins

    async with manager.lease() as adopted:
        assert "vendor.observer" in adopted.plugins

    await manager.close()
