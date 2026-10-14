from __future__ import annotations

import json
from pathlib import Path

from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent.inventory import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InstalledCapability,
    InventorySnapshot,
)
from bianbu_agent.mcp.sources import MCPSourceCatalog, runtime_mcp_id
from bianbu_agent.plugins import PluginContext


def _source(
    root: Path,
    public_id: str,
    *,
    owner: str | None = None,
) -> InstalledCapability:
    root.mkdir(parents=True)
    (root / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": "fake"}),
        encoding="utf-8",
    )
    return InstalledCapability(
        source=CapabilitySource(
            kind=CapabilityKind.MCP,
            capability_id=public_id,
            version="0.0.0",
            root=root,
            manifest_path=root / "mcp.json",
            content_digest=digest_directory(root, regular_files(root)),
            origin=(
                CapabilityOrigin.PLUGIN
                if owner is not None
                else CapabilityOrigin.STANDALONE
            ),
            plugin_id=owner,
        ),
        enabled=True,
    )


def test_source_catalog_assigns_stable_internal_runtime_ids(tmp_path: Path) -> None:
    standalone = _source(tmp_path / "mcps" / "mcp.weather", "mcp.weather")
    contributed = _source(
        tmp_path / "plugins" / "office.core" / "mcps" / "filesystem",
        "office.core/filesystem",
        owner="office.core",
    )

    catalog = MCPSourceCatalog.from_snapshot(
        InventorySnapshot(revision=1, items=(standalone, contributed))
    )

    assert set(catalog.records) == {"mcp.weather", "office.core/filesystem"}
    weather = catalog.records["mcp.weather"]
    filesystem = catalog.records["office.core/filesystem"]
    assert weather.runtime_id == runtime_mcp_id("mcp.weather")
    assert filesystem.runtime_id == runtime_mcp_id("office.core/filesystem")
    assert weather.source_kind == "standalone"
    assert filesystem.source_kind == "plugin"
    assert filesystem.owner_plugin_id == "office.core"
    assert weather.runtime_id != filesystem.runtime_id


def test_source_catalog_builds_adapter_with_public_identity(tmp_path: Path) -> None:
    installed = _source(tmp_path / "mcps" / "mcp.weather", "mcp.weather")
    catalog = MCPSourceCatalog.from_snapshot(
        InventorySnapshot(revision=1, items=(installed,))
    )

    candidate = catalog.candidates(workspace=tmp_path / "workspace")[0]
    instance = candidate.factory(
        PluginContext(plugin_id=candidate.manifest.id, data_dir=tmp_path / "data")
    )

    assert candidate.manifest.id == runtime_mcp_id("mcp.weather")
    assert candidate.source == "mcp:mcp.weather"
    assert instance.mcp_id == "mcp.weather"
    assert instance.runtime_id == candidate.manifest.id
