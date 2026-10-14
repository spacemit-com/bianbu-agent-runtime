import logging
from pathlib import Path
import json

import pytest

from bianbu_agent.inventory import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InstalledCapability,
    InventorySnapshot,
)
from bianbu_agent.plugins import DirectoryPluginDiscovery
from bianbu_agent.plugins import PluginContext
from bianbu_agent.plugins.scope import PluginScope
from bianbu_agent.mcp.plugin import MCPDirectoryPlugin


def _plugin(root: Path, plugin_id: str = "vendor.observer") -> None:
    skill = root / "skills" / "review"
    skill.mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        """\
schema_version: 1
name: {plugin_id}
version: 1.2.0
description: Directory Plugin with one Skill.
""".format(plugin_id=plugin_id),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review\nversion: 1.0.0\n---\nReview.",
        encoding="utf-8",
    )


def _installed(
    root: Path,
    *,
    plugin_id: str = "vendor.observer",
    enabled: bool = True,
) -> InstalledCapability:
    return InstalledCapability(
        source=CapabilitySource(
            kind=CapabilityKind.PLUGIN,
            capability_id=plugin_id,
            version="1.2.0",
            root=root,
            manifest_path=root / "plugin.yaml",
            content_digest="a" * 64,
            origin=CapabilityOrigin.STANDALONE,
        ),
        enabled=enabled,
    )


def _installed_mcp(root: Path, *, mcp_id: str = "mcp.weather") -> InstalledCapability:
    return InstalledCapability(
        source=CapabilitySource(
            kind=CapabilityKind.MCP,
            capability_id=mcp_id,
            version="0.0.0",
            root=root,
            manifest_path=root / "mcp.json",
            content_digest="b" * 64,
            origin=CapabilityOrigin.STANDALONE,
        ),
        enabled=True,
    )


@pytest.mark.asyncio
async def test_directory_plugin_discovery_creates_real_manifest_candidates(
    tmp_path: Path,
) -> None:
    enabled_root = tmp_path / "enabled" / "vendor.observer"
    disabled_root = tmp_path / "disabled" / "vendor.disabled"
    _plugin(enabled_root)
    _plugin(disabled_root, "vendor.disabled")

    candidates = DirectoryPluginDiscovery().discover(
        InventorySnapshot(
            revision=1,
            items=(
                _installed(enabled_root),
                _installed(
                    disabled_root,
                    plugin_id="vendor.disabled",
                    enabled=False,
                ),
            ),
        )
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.manifest.id == "vendor.observer"
    assert candidate.manifest.version == "1.2.0"
    assert candidate.manifest.dependencies == ()
    assert dict(candidate.manifest.config_schema) == {}
    assert candidate.source == f"directory:{enabled_root}"
    assert candidate.manifest.contributions.skills == ("review",)
    scope = PluginScope("vendor.observer")
    context = PluginContext(
        plugin_id="vendor.observer",
        data_dir=tmp_path / "data",
        scope=scope,
    )
    instance = candidate.factory(context)
    assert await instance.setup(context) is None
    assert tuple(item.spec.name for item in scope.snapshot().skills) == ("review",)


def test_directory_plugin_discovery_skips_inventory_manifest_id_mismatch(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "vendor.other"
    _plugin(root, "vendor.other")

    with caplog.at_level(logging.WARNING, logger="bianbu_agent.plugins.discovery"):
        candidates = DirectoryPluginDiscovery().discover(
            InventorySnapshot(revision=1, items=(_installed(root),))
        )

    assert candidates == ()
    assert "does not match its Inventory entry" in caplog.text


def test_directory_plugin_discovery_selects_adapter_for_first_class_mcp_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mcp.weather"
    root.mkdir()
    (root / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": "server"}),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"

    candidate = DirectoryPluginDiscovery(workspace_root=workspace).discover(
        InventorySnapshot(revision=1, items=(_installed_mcp(root),))
    )[0]
    instance = candidate.factory(
        PluginContext(plugin_id=candidate.manifest.id, data_dir=tmp_path / "data")
    )

    assert isinstance(instance, MCPDirectoryPlugin)
    assert instance.mcp_id == "mcp.weather"
