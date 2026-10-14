from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
from textwrap import dedent

import pytest

from bianbu_agent.config import RuntimeConfig
from bianbu_agent.inventory import CapabilityKind
from bianbu_agent.server.runtime import _inventory_snapshot


def _write_skill(root: Path, body: str) -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(body, encoding="utf-8")


def _write_mcp(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": "weather-mcp"}),
        encoding="utf-8",
    )


def test_inventory_discovers_standalone_mcp_as_first_class_source(
    tmp_path: Path,
) -> None:
    mcps = tmp_path / "mcps"
    root = mcps / "mcp.weather"
    _write_mcp(root)
    config = RuntimeConfig(
        data_dir=tmp_path,
        mcp_dirs=(mcps,),
        skill_dirs=(),
        plugin_dirs=(),
    )

    snapshot = _inventory_snapshot(config)

    items = [item for item in snapshot.items if item.source.kind is CapabilityKind.MCP]
    assert len(items) == 1
    item = items[0]
    assert item.source.capability_id == "mcp.weather"
    assert item.source.root == root.resolve()
    assert item.source.manifest_path == (root / "mcp.json").resolve()
    assert item.source.origin.value == "standalone"
    assert item.source.plugin_id is None


def test_inventory_discovers_unowned_valid_skill_when_unmanaged_is_disabled(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    _write_skill(
        skills / "review",
        "---\nname: review\ndescription: Review specs\n---\nReview carefully.",
    )
    config = RuntimeConfig(
        data_dir=tmp_path,
        skill_dirs=(skills,),
        plugin_dirs=(),
        allow_unmanaged_capabilities=False,
    )

    snapshot = _inventory_snapshot(config)

    assert [
        (item.source.kind, item.source.capability_id) for item in snapshot.items
    ] == [(CapabilityKind.SKILL, "review")]


@pytest.mark.parametrize(
    "invalid_manifest",
    (
        "---\nname: invalid\n---\nMissing description.",
        "---\nname: invalid\ndescription:\n---\nEmpty description.",
    ),
)
def test_inventory_warns_and_skips_invalid_skill_without_blocking_neighbors(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    invalid_manifest: str,
) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills / "invalid", invalid_manifest)
    _write_skill(
        skills / "valid",
        "---\nname: valid\ndescription: Valid skill\n---\nProceed.",
    )
    config = RuntimeConfig(
        data_dir=tmp_path,
        skill_dirs=(skills,),
        plugin_dirs=(),
        allow_unmanaged_capabilities=False,
    )

    with caplog.at_level(logging.WARNING, logger="bianbu_agent.server.runtime"):
        snapshot = _inventory_snapshot(config)

    assert [item.source.capability_id for item in snapshot.items] == ["valid"]
    assert str(skills / "invalid") in caplog.text
    assert "frontmatter description is required" in caplog.text


def _write_plugin_manifest(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(
        f'''schema_version: 1
name: {root.name}
version: 1.0.0
''',
        encoding="utf-8",
    )


def test_inventory_discovers_strict_directory_plugin(
    tmp_path: Path,
) -> None:
    plugins = tmp_path / "plugins"
    root = plugins / "vendor.observer"
    (root / "skills").mkdir(parents=True)
    _write_plugin_manifest(root)
    config = RuntimeConfig(
        data_dir=tmp_path,
        skill_dirs=(),
        plugin_dirs=(plugins,),
        allow_unmanaged_capabilities=False,
    )

    snapshot = _inventory_snapshot(config)

    assert len(snapshot.items) == 1
    item = snapshot.items[0]
    assert item.source.kind is CapabilityKind.PLUGIN
    assert item.source.capability_id == "vendor.observer"
    assert item.source.version == "1.0.0"
    assert item.source.manifest_path == root / "plugin.yaml"
    assert item.source.metadata == {"discovered": True}


def test_inventory_derives_plugin_owned_mcp_sources(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugin = plugins / "vendor.bundle"
    weather = plugin / "mcps" / "weather"
    filesystem = plugin / "mcps" / "filesystem"
    _write_mcp(weather)
    _write_mcp(filesystem)
    _write_plugin_manifest(plugin)
    config = RuntimeConfig(
        data_dir=tmp_path,
        mcp_dirs=(tmp_path / "mcps",),
        skill_dirs=(),
        plugin_dirs=(plugins,),
    )

    snapshot = _inventory_snapshot(config)

    mcps = {
        item.source.capability_id: item.source
        for item in snapshot.items
        if item.source.kind is CapabilityKind.MCP
    }
    assert set(mcps) == {"vendor.bundle/filesystem", "vendor.bundle/weather"}
    assert mcps["vendor.bundle/weather"].origin.value == "plugin"
    assert mcps["vendor.bundle/weather"].plugin_id == "vendor.bundle"
    assert mcps["vendor.bundle/weather"].root == weather.resolve()
    assert mcps["vendor.bundle/weather"].manifest_path == (
        weather / "mcp.json"
    ).resolve()


def test_inventory_filesystem_plugin_replaces_stale_manifest_entry(
    tmp_path: Path,
) -> None:
    plugins = tmp_path / "plugins"
    root = plugins / "vendor.observer"
    (root / "skills").mkdir(parents=True)
    _write_plugin_manifest(root)
    connection = sqlite3.connect(tmp_path / "inventory.sqlite3")
    connection.execute("CREATE TABLE inventory_metadata (key TEXT, value TEXT)")
    connection.execute(
        "INSERT INTO inventory_metadata (key, value) VALUES ('revision', '9')"
    )
    connection.execute(
        """CREATE TABLE installed_capabilities (
            kind TEXT, capability_id TEXT, version TEXT, root TEXT,
            manifest_path TEXT, content_digest TEXT, origin TEXT,
            plugin_id TEXT, metadata_json TEXT, enabled INTEGER
        )"""
    )
    connection.execute(
        "INSERT INTO installed_capabilities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "plugin",
            "vendor.observer",
            "1.0.0",
            str(root),
            str(root / "plugin.toml"),
            "0" * 64,
            "standalone",
            None,
            json.dumps({}),
            1,
        ),
    )
    connection.commit()
    connection.close()

    snapshot = _inventory_snapshot(
        RuntimeConfig(
            data_dir=tmp_path,
            skill_dirs=(),
                plugin_dirs=(plugins,),
        )
    )

    assert snapshot.revision == 9
    assert len(snapshot.items) == 1
    item = snapshot.items[0]
    assert item.source.version == "1.0.0"
    assert item.source.manifest_path == root / "plugin.yaml"
    assert item.source.content_digest != "0" * 64


def test_inventory_ignores_a_placeholder_file_without_schema(tmp_path: Path) -> None:
    """没有托管 schema 的 inventory.sqlite3 不是能力目录。

    K3 用户目录里留下过一个 0 字节的占位文件（历史写入者创建、当前没有任何代码
    写它）。把它当成损坏的 Inventory 会让每个能力请求都 422，专家页因此显示
    runtime 不可用；空文件与缺 schema 的文件都按空目录处理。
    """

    (tmp_path / "inventory.sqlite3").write_bytes(b"")
    config = RuntimeConfig(
        data_dir=tmp_path,
        skill_dirs=(),
        plugin_dirs=(),
    )

    assert _inventory_snapshot(config).items == ()


def test_inventory_rejects_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    (tmp_path / "inventory.sqlite3").write_bytes(b"this file is not sqlite")
    config = RuntimeConfig(
        data_dir=tmp_path,
        skill_dirs=(),
        plugin_dirs=(),
    )

    with pytest.raises(ValueError, match="invalid capability Inventory"):
        _inventory_snapshot(config)
