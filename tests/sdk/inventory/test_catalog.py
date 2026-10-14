from pathlib import Path
from textwrap import dedent

import pytest

from bianbu_agent.tools.builtins import BUILTIN_MODULES

from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent.inventory import (
    CapabilityCatalogBuilder,
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InstalledCapability,
    InventorySnapshot,
)
from bianbu_agent.plugins import (
    PluginContributions,
    PluginGeneration,
    PluginHealth,
    PluginRuntime,
    PluginState,
    PluginStatus,
    ToolContribution,
    ToolResult,
    ToolSpec,
)
from bianbu_agent.tools import DirectoryToolLoader, ToolLoadContext


def _skill(root: Path, name: str) -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\nversion: 1.0.0\n---\n"
        f"Use {name} carefully.",
        encoding="utf-8",
    )


def _tool(root: Path, name: str, owner: str) -> None:
    root.mkdir(parents=True)
    (root / "tool.py").write_text(
        dedent(
            f"""
            from bianbu_agent.tools import tool

            def create_tool(context):
                @tool
                def {name}() -> str:
                    \"\"\"Run {name}.\"\"\"
                    return "{name}"
                return {name}
            """
        ),
        encoding="utf-8",
    )


def _plugin_manifest(root: Path) -> None:
    """Write the identity-only manifest; members come from the directories."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(
        "\n".join(
            (
                "schema_version: 1",
                f"name: {root.name}",
                "version: 1.0.0",
                "description: Catalog fixture plugin.",
                "",
            )
        ),
        encoding="utf-8",
    )


def _installed(
    kind: CapabilityKind,
    capability_id: str,
    root: Path,
    manifest: str | None,
    *,
    enabled: bool = True,
    origin: CapabilityOrigin = CapabilityOrigin.STANDALONE,
) -> InstalledCapability:
    files = tuple(
        path
        for path in regular_files(root)
        if path.relative_to(root).parts[:1] != ("data",)
    )
    return InstalledCapability(
        source=CapabilitySource(
            kind=kind,
            capability_id=capability_id,
            version="1.0.0",
            root=root,
            manifest_path=root if manifest is None else root / manifest,
            content_digest=digest_directory(root, files),
            origin=origin,
        ),
        enabled=enabled,
    )


def test_catalog_loads_builtin_tools_without_tool_inventory_records(
    tmp_path: Path,
) -> None:
    catalog = CapabilityCatalogBuilder(
        workspace_root=tmp_path / "workspace",
        home_root=tmp_path,
    ).build(InventorySnapshot(revision=4))

    assert set(catalog.tools) == set(BUILTIN_MODULES)
    source = catalog.sources[(CapabilityKind.TOOL, "read_file")]
    assert source.metadata["builtin"] is True
    assert source.root.name == "files"
    assert source.metadata["directory_catalog"] is True


def test_catalog_combines_standalone_and_enabled_plugin_capabilities(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills" / "research"
    plugin_root = tmp_path / "plugins" / "vendor.system"
    _skill(skill_root, "research")
    plugin_root.mkdir(parents=True)
    _skill(plugin_root / "skills" / "system-diagnosis", "system-diagnosis")
    _plugin_manifest(plugin_root)
    snapshot = InventorySnapshot(
        revision=7,
        items=(
            _installed(CapabilityKind.SKILL, "research", skill_root, "SKILL.md"),
            _installed(
                CapabilityKind.PLUGIN, "vendor.system", plugin_root, "plugin.yaml"
            ),
        ),
    )

    catalog = CapabilityCatalogBuilder(
        workspace_root=tmp_path / "workspace",
        home_root=tmp_path,
    ).build(snapshot)

    assert set(catalog.tools) == set(BUILTIN_MODULES)
    assert set(catalog.skills) == {"research"}
    assert catalog.inventory_revision == 7
    assert catalog.sources[(CapabilityKind.SKILL, "research")].root == (
        skill_root.resolve()
    )
    assert tuple(catalog.plugin_sources) == ("vendor.system",)


def test_catalog_omits_disabled_plugin_and_all_of_its_children(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins" / "vendor.system"
    plugin_root.mkdir(parents=True)
    _tool(plugin_root / "tools" / "read_logs", "read_logs", "vendor.system")
    _skill(plugin_root / "skills" / "diagnosis", "diagnosis")
    _plugin_manifest(plugin_root)
    snapshot = InventorySnapshot(
        revision=2,
        items=(
            _installed(
                CapabilityKind.PLUGIN,
                "vendor.system",
                plugin_root,
                "plugin.yaml",
                enabled=False,
            ),
        ),
    )

    catalog = CapabilityCatalogBuilder(
        workspace_root=tmp_path / "workspace"
    ).build(snapshot)

    assert set(catalog.tools) == set(BUILTIN_MODULES)
    assert catalog.skills == {}


@pytest.mark.parametrize("kind", [CapabilityKind.SKILL])
def test_catalog_keeps_plugin_packages_separate_from_standalone_capabilities(
    tmp_path: Path,
    kind: CapabilityKind,
) -> None:
    top = tmp_path / kind.value / "shared"
    plugin = tmp_path / "plugins" / "vendor.system"
    plugin.mkdir(parents=True)
    if kind is CapabilityKind.TOOL:
        _tool(top, "shared", "bianbu.catalog.filesystem")
        _tool(plugin / "tools" / "shared", "shared", "vendor.system")
        manifest = "tool.py"
    else:
        _skill(top, "shared")
        _skill(plugin / "skills" / "shared", "shared")
        manifest = "SKILL.md"
    _plugin_manifest(plugin)
    snapshot = InventorySnapshot(
        revision=1,
        items=(
            _installed(kind, "shared", top, manifest),
            _installed(
                CapabilityKind.PLUGIN, "vendor.system", plugin, "plugin.yaml"
            ),
        ),
    )

    catalog = CapabilityCatalogBuilder(
        workspace_root=tmp_path / "workspace"
    ).build(snapshot)

    values = catalog.tools if kind is CapabilityKind.TOOL else catalog.skills
    assert set(values) == {"shared"}
    assert tuple(catalog.plugin_sources) == ("vendor.system",)


def test_catalog_records_plugin_source_without_importing_embedded_tools(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugins" / "vendor.system"
    plugin.mkdir(parents=True)
    _tool(plugin / "tools" / "shared", "shared", "vendor.system")
    _plugin_manifest(plugin)
    snapshot = InventorySnapshot(
        revision=1,
        items=(
            _installed(
                CapabilityKind.PLUGIN, "vendor.system", plugin, "plugin.yaml"
            ),
        ),
    )

    catalog = CapabilityCatalogBuilder(
        workspace_root=tmp_path / "workspace",
        home_root=tmp_path,
    ).build(snapshot)

    assert set(catalog.tools) == set(BUILTIN_MODULES)
    assert catalog.plugin_sources["vendor.system"].root == plugin.resolve()


def test_plugin_tool_loader_rejects_mismatched_directory_name(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugins/vendor.system"
    plugin.mkdir(parents=True)
    _tool(plugin / "tools/clock", "timer", "vendor.system")
    _plugin_manifest(plugin)

    with pytest.raises(ValueError, match="does not match decorated Tool name"):
        DirectoryToolLoader().load(
            plugin / "tools/clock",
            context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
            plugin_id="vendor.system",
        )


class _Plugin:
    async def stop(self) -> None:
        return None


async def _hidden_handler(_context, _arguments):
    return ToolResult("hidden")


def test_strict_generation_rejects_hidden_lifecycle_tools(tmp_path: Path) -> None:
    catalog = CapabilityCatalogBuilder(
        workspace_root=tmp_path / "workspace"
    ).build(InventorySnapshot(revision=0))
    plugin_id = "vendor.hidden"
    runtime = PluginRuntime(
        plugin_id=plugin_id,
        instance=_Plugin(),
        config={},
        contributions=PluginContributions(
            tools=(
                ToolContribution(
                    plugin_id=plugin_id,
                    spec=ToolSpec("hidden", "Hidden", {"type": "object"}),
                    handler=_hidden_handler,
                ),
            )
        ),
    )

    with pytest.raises(ValueError, match="file-backed catalog"):
        PluginGeneration(
            id=1,
            plugins={plugin_id: runtime},
            statuses={
                plugin_id: PluginStatus(
                    plugin_id=plugin_id,
                    state=PluginState.RUNNING,
                    health=PluginHealth.healthy(),
                )
            },
            capability_catalog=catalog,
        )
