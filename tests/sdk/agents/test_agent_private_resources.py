"""Capabilities that live inside one Agent package."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from bianbu_agent import AgentManager, AgentSpec, InMemoryAgentRegistry, ModelRegistry
from bianbu_agent.agents.private_resources import (
    discover_private_resources,
    overlay_private_resources,
)
from bianbu_agent.inventory import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InstalledCapability,
)
from bianbu_agent.plugins import SkillContribution, SkillSpec
from bianbu_agent.server.runtime import _inventory_snapshot


@dataclass
class FakeModel:
    model: str


@dataclass
class Generation:
    tools: dict = field(default_factory=dict)
    skills: dict = field(default_factory=dict)
    mcps: dict = field(default_factory=dict)
    mcp_tools: dict = field(default_factory=dict)
    hooks: dict = field(default_factory=dict)
    plugins: dict = field(default_factory=dict)
    capability_catalog: object | None = None


def _skill(root: Path, name: str, *, description: str = "Private skill") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1.2.3\n---\n\nUse it.\n",
        encoding="utf-8",
    )
    return directory


def _mcp(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "mcp.json").write_text(
        '{"transport": "stdio", "command": "echo"}',
        encoding="utf-8",
    )
    return directory


def _plugin(root: Path, name: str) -> Path:
    directory = root / name
    (directory / "skills" / "local").mkdir(parents=True)
    (directory / "skills" / "local" / "SKILL.md").write_text(
        "---\nname: local\ndescription: Local.\n---\n\nLocal.\n",
        encoding="utf-8",
    )
    (directory / "plugin.yaml").write_text(
        f"schema_version: 1\nname: {name}\nversion: 1.0.0\ndescription: Private.\n",
        encoding="utf-8",
    )
    return directory


def _agent(tmp_path: Path, agent_id: str) -> Path:
    root = tmp_path / "runtime" / "agents" / agent_id
    root.mkdir(parents=True)
    (root / "agent.yaml").write_text(
        "schema_version: 2\n"
        "agent:\n"
        f"  id: {agent_id}\n"
        "  version: 1.0.0\n"
        "  name: Private Expert\n",
        encoding="utf-8",
    )
    return root


def _catalog(owners: dict) -> object:
    return SimpleNamespace(
        private_owners=owners, mcp_sources={}, plugin_sources={}
    )


def _contribution(name: str, plugin_id: str = "catalog.test") -> SkillContribution:
    return SkillContribution(
        plugin_id=plugin_id,
        spec=SkillSpec(name=name, description=name, instructions="Do it"),
    )


def _source(kind: CapabilityKind, capability_id: str) -> CapabilitySource:
    return CapabilitySource(
        kind=kind,
        capability_id=capability_id,
        version="1.0.0",
        root=Path("/tmp") / capability_id,
        manifest_path=Path("/tmp") / capability_id / "x",
        content_digest="0" * 64,
        origin=CapabilityOrigin.STANDALONE,
    )


def test_private_trees_are_discovered_from_the_agent_package(tmp_path: Path) -> None:
    root = _agent(tmp_path, "com.bianbu.private")
    _skill(root / "skills", "private-skill")
    _mcp(root / "mcps", "private-mcp")
    _plugin(root / "plugins", "private.plugin")

    private = discover_private_resources(root, "com.bianbu.private")

    assert private.skill_ids == ("private-skill",)
    assert private.mcp_ids == ("private-mcp",)
    assert private.plugin_ids == ("private.plugin",)
    for item in private.items:
        assert item.source.metadata["agent_private"] is True
        assert item.source.metadata["owner_agent_id"] == "com.bianbu.private"


def test_apm_submitted_mcp_and_plugin_subresources_are_discovered(
    tmp_path: Path,
) -> None:
    """APM Expert packages use apc-plugin.json and flat mcp/<name>.json."""

    root = _agent(tmp_path, "com.bianbu.apm")
    _skill(root / "skills", "private-skill")
    (root / "mcp").mkdir()
    (root / "mcp" / "filesystem.json").write_text(
        '{"schema_version": 2, "name": "filesystem", "name_en": "Filesystem",'
        ' "version": "v0.1.0", "description": "Files", "description_zh": "Files",'
        ' "definition": {"transport": "stdio", "command": "echo"}}',
        encoding="utf-8",
    )
    plugin = root / "plugins" / "demo-plugin"
    (plugin / "skills" / "demo-plugin").mkdir(parents=True)
    (plugin / "skills" / "demo-plugin" / "SKILL.md").write_text(
        "---\nname: demo-plugin\ndescription: Demo.\n---\n\nDemo.\n",
        encoding="utf-8",
    )
    (plugin / "apc-plugin.json").write_text(
        '{"schema_version": 2, "name": "demo-plugin", "name_zh": "演示插件",'
        ' "name_en": "Demo Plugin", "version": "v0.1.0",'
        ' "description": "Demo.", "description_zh": "演示。",'
        ' "skills": ["skills/demo-plugin"]}',
        encoding="utf-8",
    )

    private = discover_private_resources(root, "com.bianbu.apm")

    assert private.skill_ids == ("private-skill",)
    assert private.mcp_ids == ("filesystem",)
    assert private.plugin_ids == ("demo-plugin",)
    mcp = next(
        item for item in private.items
        if item.source.kind is CapabilityKind.MCP
    )
    assert mcp.source.manifest_path == (root / "mcp" / "filesystem.json").resolve()

    from bianbu_agent.config import RuntimeConfig

    snapshot = _inventory_snapshot(
        RuntimeConfig(data_dir=tmp_path / "runtime"), private=private
    )
    present = {
        (item.source.kind, item.source.capability_id)
        for item in snapshot.items
    }
    assert (CapabilityKind.MCP, "filesystem") in present
    assert (CapabilityKind.PLUGIN, "demo-plugin") in present


def test_private_trees_reject_symlinks(tmp_path: Path) -> None:
    root = _agent(tmp_path, "com.bianbu.private")
    skills = root / "skills"
    skills.mkdir()
    (skills / "escape").symlink_to(tmp_path)

    with pytest.raises(ValueError, match="regular directories"):
        discover_private_resources(root, "com.bianbu.private")


def test_private_entries_shadow_device_wide_ones(tmp_path: Path) -> None:
    private = discover_private_resources(
        _skilled_agent(tmp_path), "com.bianbu.private"
    )
    global_item = InstalledCapability(
        source=_source(CapabilityKind.SKILL, "shared"),
        enabled=True,
    )

    merged = overlay_private_resources((global_item,), private)

    shared = [item for item in merged if item.source.capability_id == "shared"]
    assert len(shared) == 1
    assert shared[0].source.metadata["agent_private"] is True


def test_private_mcp_shadow_removes_stale_plugin_members(tmp_path: Path) -> None:
    root = _agent(tmp_path, "com.bianbu.private")
    _mcp(root / "mcps", "shared")
    plugin = root / "plugins" / "vendor.bundle"
    (plugin / "mcps" / "shared").mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        "schema_version: 1\nname: vendor.bundle\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (plugin / "mcps" / "shared" / "mcp.json").write_text(
        '{"transport": "stdio", "command": "echo"}', encoding="utf-8"
    )
    private = discover_private_resources(root, "com.bianbu.private")
    from bianbu_agent.config import RuntimeConfig

    from bianbu_agent.config import RuntimeConfig

    snapshot = _inventory_snapshot(
        RuntimeConfig(data_dir=tmp_path / "runtime"), private=private
    )

    mcp_ids = [
        item.source.capability_id
        for item in snapshot.items
        if item.source.kind is CapabilityKind.MCP
    ]
    assert "shared" in mcp_ids
    assert "vendor.bundle/shared" in mcp_ids
    shadowed = next(
        item for item in snapshot.items
        if item.source.kind is CapabilityKind.MCP
        and item.source.capability_id == "shared"
    )
    assert shadowed.source.metadata["agent_private"] is True


def _skilled_agent(tmp_path: Path) -> Path:
    root = _agent(tmp_path, "com.bianbu.private")
    _skill(root / "skills", "shared")
    return root


@pytest.mark.asyncio
async def test_private_capabilities_are_locked_into_the_agent(tmp_path: Path) -> None:
    generation = Generation(
        skills={"private-skill": _contribution("private-skill")},
        plugins={"private.plugin": object()},
        mcps={"private-mcp": object()},
        mcp_tools={"private-mcp": ("mcp_tool",)},
        capability_catalog=_catalog(
            {
                (CapabilityKind.SKILL, "private-skill"): "expert",
                (CapabilityKind.PLUGIN, "private.plugin"): "expert",
                (CapabilityKind.MCP, "private-mcp"): "expert",
            }
        ),
    )
    manager = AgentManager(
        store=InMemoryAgentRegistry(),
        model_registry=ModelRegistry(
            default_model=FakeModel("default"),
            models={"default": FakeModel("default")},
        ),
        generation_provider=lambda: generation,
    )
    spec = await manager.create(
        AgentSpec(id="expert", name="Expert", model="default")
    )

    resolved = await manager.materialize(spec.id, generation)

    assert resolved.skills == ("private-skill",)
    assert resolved.plugins == ("private.plugin",)
    assert resolved.mcps == ("private-mcp",)
    assert "mcp_tool" in resolved.tools
    # The stored declaration is untouched; the forced selection is a Runtime
    # view, exactly like a preset.
    assert (await manager.get("expert")).skills == ()


def test_runtime_snapshot_overlays_private_skills(tmp_path: Path) -> None:
    from bianbu_agent.config import RuntimeConfig

    root = _agent(tmp_path, "com.bianbu.private")
    _skill(root / "skills", "private-skill")
    private = discover_private_resources(root, "com.bianbu.private")
    config = RuntimeConfig(data_dir=tmp_path / "runtime")

    snapshot = _inventory_snapshot(config, private=private)

    skills = [
        item for item in snapshot.items if item.source.kind is CapabilityKind.SKILL
    ]
    # The device may also carry global Skills; the private tree only adds its
    # own entry and must never duplicate or lose it.
    private_skills = [
        item for item in skills if item.source.capability_id == "private-skill"
    ]
    assert len(private_skills) == 1
    assert private_skills[0].source.metadata["agent_private"] is True
