from __future__ import annotations

from pathlib import Path

import pytest

from bianbu_agent import AgentHome
from bianbu_agent.plugins import PluginInstaller
from bianbu_agent.plugins.manifest import load_plugin_manifest
from bianbu_agent.plugins.models import PluginContributionManifest


def _write_manifest(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(body, encoding="utf-8")


def _member(root: Path, kind: str, name: str) -> Path:
    member = root / kind / name
    member.mkdir(parents=True, exist_ok=True)
    return member


def test_loads_identity_and_scans_contributions(tmp_path: Path) -> None:
    root = tmp_path / "vendor.bundle"
    _write_manifest(
        root,
        """\
schema_version: 1
name: vendor.bundle
version: 2.3.1
description: Example bundle
dependencies:
  - name: vendor.base
    version: ">=1.2"
config_schema:
  type: object
  properties:
    enabled:
      type: boolean
      default: true
""",
    )
    (_member(root, "tools", "echo") / "tool.py").write_text("", encoding="utf-8")
    (_member(root, "skills", "review") / "SKILL.md").write_text("", encoding="utf-8")
    (_member(root, "hooks", "audit.after_run") / "hook.json").write_text("", encoding="utf-8")
    (_member(root, "mcps", "weather") / "mcp.json").write_text("", encoding="utf-8")

    manifest = load_plugin_manifest(root)

    assert manifest.id == "vendor.bundle"
    assert manifest.version == "2.3.1"
    assert manifest.description == "Example bundle"
    assert manifest.dependencies[0].plugin_id == "vendor.base"
    assert manifest.contributions.tools == ("echo",)
    assert manifest.contributions.skills == ("review",)
    assert manifest.contributions.hooks == ("audit.after_run",)
    assert manifest.contributions.mcps == ("weather",)
    assert manifest.config_schema["properties"]["enabled"]["default"] is True


def test_package_without_contributions_is_valid(tmp_path: Path) -> None:
    root = tmp_path / "vendor.empty"
    _write_manifest(
        root,
        'schema_version: 1\nname: vendor.empty\nversion: 1.0.0\n',
    )

    manifest = load_plugin_manifest(root)

    assert manifest.contributions == PluginContributionManifest()


def test_manifest_is_required_and_unknown_fields_fail_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="plugin.yaml is required"):
        load_plugin_manifest(tmp_path)

    root = tmp_path / "unknown"
    _write_manifest(
        root,
        """\
schema_version: 1
name: vendor.bundle
version: 1.0.0
surprise: true
""",
    )
    with pytest.raises(ValueError, match="unknown plugin.yaml key: surprise"):
        load_plugin_manifest(root)


@pytest.mark.parametrize(
    "removed",
    (
        "id: vendor.bundle",
        'sdk_version: ">=0.1,<1"',
        "permissions: [tool.provide]",
        "contributions:\n  tools: [echo]",
    ),
)
def test_removed_manifest_keys_are_rejected(tmp_path: Path, removed: str) -> None:
    root = tmp_path / "vendor.removed"
    _write_manifest(
        root,
        f"schema_version: 1\nname: vendor.removed\nversion: 1.0.0\n{removed}\n",
    )

    with pytest.raises(ValueError, match="unknown plugin.yaml key:"):
        load_plugin_manifest(root)


def test_dependency_entries_use_the_plugin_name_key(tmp_path: Path) -> None:
    root = tmp_path / "vendor.dependent"
    _write_manifest(
        root,
        """\
schema_version: 1
name: vendor.dependent
version: 1.0.0
dependencies:
  - id: vendor.base
    version: ">=1.2"
""",
    )
    with pytest.raises(ValueError, match="unknown dependency key: id"):
        load_plugin_manifest(root)


def test_manifest_requires_schema_version_name_and_version(tmp_path: Path) -> None:
    version = tmp_path / "version"
    _write_manifest(version, "schema_version: 1\nname: vendor.bundle\nversion: 1.0\n")
    with pytest.raises(ValueError, match="version must be a string"):
        load_plugin_manifest(version)

    name = tmp_path / "name"
    _write_manifest(name, "schema_version: 1\nversion: 1.0.0\n")
    with pytest.raises(ValueError, match="name must be a string"):
        load_plugin_manifest(name)

    schema = tmp_path / "schema"
    _write_manifest(schema, "name: vendor.bundle\nversion: 1.0.0\n")
    with pytest.raises(ValueError, match="schema_version must be 1"):
        load_plugin_manifest(schema)


def test_duplicate_member_name_across_kinds_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "vendor.bundle"
    _write_manifest(root, "schema_version: 1\nname: vendor.bundle\nversion: 1.0.0\n")
    _member(root, "tools", "echo")
    _member(root, "skills", "echo")

    with pytest.raises(ValueError, match="duplicate contribution name: echo"):
        load_plugin_manifest(root)


def test_member_directory_must_be_a_real_directory(tmp_path: Path) -> None:
    root = tmp_path / "vendor.bundle"
    _write_manifest(root, "schema_version: 1\nname: vendor.bundle\nversion: 1.0.0\n")
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "review").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="skills members must be regular directories"):
        load_plugin_manifest(root)


def test_installer_rejects_missing_entrypoint_file(tmp_path: Path) -> None:
    source = tmp_path / "vendor.entrypoint"
    _write_manifest(
        source,
        """\
schema_version: 1
name: vendor.entrypoint
version: 1.0.0
entrypoint: activate.py:setup
""",
    )

    with pytest.raises(ValueError, match="entrypoint.*missing"):
        PluginInstaller(AgentHome(tmp_path / ".bianbu-agent")).install(source)


def test_installer_uses_manifest_identity_and_keeps_complete_package(
    tmp_path: Path,
) -> None:
    source = tmp_path / "downloaded-artifact"
    _write_manifest(
        source,
        'schema_version: 1\nname: vendor.bundle\nversion: 2.3.1\n',
    )
    tool_root = _member(source, "tools", "echo")
    (tool_root / "tool.py").write_text(
        "from bianbu_agent.tools import tool\n"
        "def create_tool(context):\n"
        "    @tool\n"
        "    def echo() -> str:\n"
        "        '''Echo.'''\n"
        "        return 'ok'\n"
        "    return echo\n",
        encoding="utf-8",
    )
    home = AgentHome(tmp_path / ".bianbu-agent")

    installed = PluginInstaller(home).install(source)

    assert installed.id == "vendor.bundle"
    assert installed.manifest.version == "2.3.1"
    assert installed.member_ids == ("vendor.bundle/echo",)
    assert (home.plugins_dir / "vendor.bundle" / "plugin.yaml").is_file()
    assert (home.plugins_dir / "vendor.bundle" / "tools" / "echo" / "tool.py").is_file()
    assert not (home.root / "tools").exists()
