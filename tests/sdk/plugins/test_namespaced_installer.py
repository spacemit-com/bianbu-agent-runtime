from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
from textwrap import dedent

import pytest
import yaml

from bianbu_agent import CapabilityInstaller, AgentHome
from bianbu_agent.plugins import PluginCatalog, PluginInstaller


def _tool(root: Path, name: str = "echo") -> None:
    root.mkdir(parents=True)
    (root / "tool.py").write_text(
        "from bianbu_agent.tools import tool\n"
        "def create_tool(context):\n"
        "    @tool\n"
        f"    def {name}() -> str:\n"
        "        '''Echo.'''\n"
        "        return 'ok'\n"
        f"    return {name}\n",
        encoding="utf-8",
    )


def _skill(root: Path, name: str = "review") -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Review carefully\n"
        "version: 1.0.0\n---\nReview carefully.\n",
        encoding="utf-8",
    )


def _hook(root: Path, hook_id: str = "audit.tool") -> None:
    root.mkdir(parents=True)
    (root / "hook.json").write_text(
        '{"schema_version":1,"hooks":{"BeforeRun":[{"hooks":[{"type":"command","command":"true"}]}]}}',
        encoding="utf-8",
    )


def _mcp(root: Path, command: str = "weather-mcp") -> None:
    root.mkdir(parents=True)
    (root / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": command}),
        encoding="utf-8",
    )


def _manifest(root: Path, plugin_id: str | None = None) -> None:
    (root / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": plugin_id or root.name,
                "version": "1.0.0",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _plugin(root: Path, plugin_id: str = "vendor.bundle") -> None:
    assert root.name == plugin_id
    _tool(root / "tools" / "echo")
    _skill(root / "skills" / "review")
    _hook(root / "hooks" / "audit.tool")
    _manifest(root, plugin_id)


def test_installs_mixed_members_as_one_namespaced_plugin_directory(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)

    installed = PluginInstaller(home).install(source)

    assert installed.id == "vendor.bundle"
    assert installed.member_ids == (
        "vendor.bundle/audit.tool",
        "vendor.bundle/echo",
        "vendor.bundle/review",
    )
    assert installed.manifest.version == "1.0.0"
    root = home.plugins_dir / "vendor.bundle"
    assert not (root / "plugin.toml").exists()
    assert (root / "plugin.yaml").is_file()
    assert (root / "tools" / "echo" / "tool.py").is_file()
    assert (root / "skills" / "review" / "SKILL.md").is_file()
    assert {path.name for path in (root / "hooks" / "audit.tool").iterdir()} == {
        "hook.json"
    }
    assert not (home.root / "tools").exists()
    assert not (home.skills_dir / "review").exists()
    assert PluginInstaller(home).inspect("vendor.bundle") == installed
    assert PluginInstaller(home).verify("vendor.bundle") is True


def test_plugin_can_contribute_multiple_first_class_mcp_servers(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    source.mkdir()
    _mcp(source / "mcps" / "weather")
    _mcp(source / "mcps" / "filesystem", "filesystem-mcp")
    _manifest(source)

    installed = PluginInstaller(home).install(source)

    assert installed.member_ids == (
        "vendor.bundle/filesystem",
        "vendor.bundle/weather",
    )
    assert {member.kind for member in installed.members} == {"mcp"}
    assert (
        home.plugins_dir / "vendor.bundle" / "mcps" / "weather" / "mcp.json"
    ).is_file()


def test_plugin_mcp_member_rejects_extra_files_and_symlinks(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    extra = tmp_path / "vendor.extra"
    extra.mkdir()
    _mcp(extra / "mcps" / "weather")
    (extra / "mcps" / "weather" / "README.md").write_text(
        "unexpected", encoding="utf-8"
    )
    _manifest(extra)
    with pytest.raises(ValueError, match="only contain mcp.json"):
        PluginInstaller(home).install(extra)

    linked = tmp_path / "vendor.linked"
    linked.mkdir()
    _mcp(linked / "real")
    (linked / "mcps").mkdir()
    (linked / "mcps" / "weather").symlink_to(linked / "real", target_is_directory=True)
    _manifest(linked)
    with pytest.raises(ValueError, match="symbolic links|regular directories"):
        PluginInstaller(home).install(linked)


def test_rejects_standalone_mcp_package_on_plugin_installer(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.mcp"
    source.mkdir()
    document = {
        "transport": "streamable-http",
        "url": "https://mcp.example.test/rpc",
        "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
    }
    (source / "mcp.json").write_text(json.dumps(document), encoding="utf-8")
    _manifest(source)

    with pytest.raises(ValueError, match="MCP catalog"):
        PluginInstaller(home).install(source)

    assert not (home.plugins_dir / "vendor.mcp").exists()


def test_rejects_invalid_mcp_json_before_publication(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.mcp"
    member = source / "mcps" / "weather"
    member.mkdir(parents=True)
    (member / "mcp.json").write_text(
        '{"transport":"stdio","command":"","unknown":true}',
        encoding="utf-8",
    )
    _manifest(source)

    with pytest.raises(ValueError, match="unknown mcp.json key: unknown"):
        PluginInstaller(home).install(source)

    assert not (home.plugins_dir / "vendor.mcp").exists()


@pytest.mark.parametrize("extra", ["console.py", "tools", "skills", "hooks", "lib"])
def test_rejects_mcp_plugin_mixed_with_local_plugin_entries(
    tmp_path: Path,
    extra: str,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.mcp"
    source.mkdir()
    (source / "mcp.json").write_text(
        '{"transport":"stdio","command":"fake"}',
        encoding="utf-8",
    )
    target = source / extra
    if "." in extra:
        target.write_text("", encoding="utf-8")
    else:
        target.mkdir()
    _manifest(source)

    with pytest.raises(ValueError, match="MCP catalog"):
        PluginInstaller(home).install(source)

    assert not (home.plugins_dir / "vendor.mcp").exists()


def test_rejects_invalid_directory_id_and_legacy_manifests(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    _manifest(invalid, "Invalid plugin")
    with pytest.raises(ValueError, match="stable lowercase"):
        PluginInstaller(home).install(invalid)

    plugin_manifest = tmp_path / "vendor.plugin-manifest"
    _plugin(plugin_manifest, "vendor.plugin-manifest")
    (plugin_manifest / "plugin.toml").write_text(
        '[plugin]\nid = "vendor.plugin-manifest"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported Plugin package entry: plugin.toml"):
        PluginInstaller(home).install(plugin_manifest)

    other_agent = tmp_path / "vendor.other-agent"
    _plugin(other_agent, "vendor.other-agent")
    (other_agent / "apc-plugin.json").write_text(
        json.dumps({"name": "vendor.other-agent", "version": "1.0.0"}),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="unsupported Plugin package entry: apc-plugin.json"
    ):
        PluginInstaller(home).install(other_agent)

    hook_manifest = tmp_path / "vendor.hook-manifest"
    _plugin(hook_manifest, "vendor.hook-manifest")
    (hook_manifest / "hooks" / "audit.tool" / "hook.toml").write_text(
        "[hook]\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="only hook.json"):
        PluginInstaller(home).install(hook_manifest)


def test_plugin_tools_remain_namespaced_and_can_share_local_names(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    first = tmp_path / "vendor.first"
    second = tmp_path / "vendor.second"
    _plugin(first, "vendor.first")
    _plugin(second, "vendor.second")
    installer = PluginInstaller(home)
    installer.install(first)
    installer.install(second)

    catalog = PluginCatalog(home)
    assert catalog.resolve("echo") is None
    assert not (home.root / "tools").exists()
    assert {item.qualified_id for item in catalog.list("tool")} == {
        "vendor.first/echo",
        "vendor.second/echo",
    }


def test_rejects_duplicate_plugin_id_and_unsafe_plugin_source_before_publication(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)
    installer = PluginInstaller(home)
    installer.install(source)

    with pytest.raises(FileExistsError, match="already installed"):
        installer.install(source)

    unsafe = tmp_path / "vendor.unsafe"
    _plugin(unsafe, "vendor.unsafe")
    (unsafe / "tools" / "echo" / "outside.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(ValueError, match="symbolic links"):
        installer.install(unsafe)
    assert not (home.plugins_dir / "vendor.unsafe").exists()


def test_installer_treats_manifest_and_member_folders_as_tool_identity(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    duplicate = tmp_path / "vendor.duplicate"
    _plugin(duplicate, "vendor.duplicate")
    _tool(duplicate / "tools" / "other", "echo")
    _manifest(duplicate)
    installed = PluginInstaller(home).install(duplicate)
    assert "vendor.duplicate/other" in installed.member_ids

    misplaced = tmp_path / "vendor.misplaced"
    _plugin(misplaced, "vendor.misplaced")
    (misplaced / "tools" / "echo").rename(misplaced / "tools" / "different")
    _manifest(misplaced)
    installed = PluginInstaller(home).install(misplaced)
    assert "vendor.misplaced/different" in installed.member_ids


def test_installer_never_imports_plugin_python_before_publication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "vendor.passive"
    tool = source / "tools" / "echo"
    hook = source / "hooks" / "audit.tool"
    tool.mkdir(parents=True)
    hook.mkdir(parents=True)
    marker = tmp_path / "imported"
    payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    (tool / "tool.py").write_text(payload, encoding="utf-8")
    (source / "scripts").mkdir()
    (source / "scripts" / "side_effect.py").write_text(payload, encoding="utf-8")
    (hook / "hook.json").write_text(json.dumps({"schema_version": 1, "hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": 'exec "${RUNTIME_PYTHON}" "${PLUGIN_ROOT}/scripts/side_effect.py"'}]}]}}), encoding="utf-8")
    _manifest(source)

    installed = PluginInstaller(AgentHome(tmp_path / ".bianbu-agent")).install(source)

    assert installed.id == "vendor.passive"
    assert not marker.exists()


def test_rejects_duplicate_local_names_across_plugin_member_kinds(tmp_path: Path) -> None:
    source = tmp_path / "vendor.cross-kind"
    _plugin(source, "vendor.cross-kind")
    _skill(source / "skills" / "echo", "echo")
    _manifest(source)

    with pytest.raises(ValueError, match="duplicate contribution name"):
        PluginInstaller(AgentHome(tmp_path / ".bianbu-agent")).install(source)


def test_failed_copy_leaves_no_active_plugin_or_private_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr("bianbu_agent.plugins.installer.shutil.copy2", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        PluginInstaller(home).install(source)

    assert not (home.plugins_dir / "vendor.bundle").exists()
    assert not (home.root / "tools").exists()
    assert not tuple(home.staging_dir.glob("plugin-*"))


def test_install_does_not_report_failure_after_atomic_plugin_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)
    installer = PluginInstaller(home)

    reads = 0

    def fail_read(*args: object, **kwargs: object):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise FileNotFoundError
        raise OSError("post-publication read must not run")

    monkeypatch.setattr(installer, "_read_plugin_at", fail_read)
    installed = installer.install(source)

    assert installed.id == "vendor.bundle"
    assert reads == 1
    assert (home.plugins_dir / "vendor.bundle").is_dir()


def test_install_suppresses_close_and_unlock_errors_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)
    installer = PluginInstaller(home)
    published = False
    original_rename = installer._capabilities._rename_noreplace
    original_close = os.close
    import bianbu_agent._internal.locking as locking

    original_flock = locking.fcntl.flock

    def mark_published(*args: object) -> None:
        nonlocal published
        original_rename(*args)
        published = True

    def fail_close(descriptor: int) -> None:
        if published:
            raise OSError("post-publish close failed")
        original_close(descriptor)

    def fail_unlock(descriptor: int, operation: int) -> None:
        if published and operation == locking.fcntl.LOCK_UN:
            raise OSError("post-publish unlock failed")
        original_flock(descriptor, operation)

    monkeypatch.setattr(installer._capabilities, "_rename_noreplace", mark_published)
    monkeypatch.setattr("bianbu_agent.plugins.installer.os.close", fail_close)
    monkeypatch.setattr(locking.fcntl, "flock", fail_unlock)

    installed = installer.install(source)

    assert installed.id == "vendor.bundle"
    assert (home.plugins_dir / "vendor.bundle").is_dir()


def test_inspect_returns_none_for_a_missing_plugin(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()

    assert PluginInstaller(home).inspect("vendor.missing") is None


def test_preserves_empty_plugin_directories_when_staging(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)
    empty = source / "skills" / "review" / "references" / "empty"
    empty.mkdir(parents=True)

    PluginInstaller(home).install(source)

    assert (home.plugins_dir / "vendor.bundle" / "skills" / "review" / "references" / "empty").is_dir()


def test_uninstall_quarantines_the_one_verified_plugin_directory(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    standalone = tmp_path / "standalone"
    _plugin(source)
    _skill(standalone, "review")
    CapabilityInstaller(home).install_skill(standalone)
    installer = PluginInstaller(home)
    installer.install(source)

    assert installer.uninstall("vendor.bundle") is True

    assert not (home.plugins_dir / "vendor.bundle").exists()
    assert CapabilityInstaller(home).verify("skill", "review") is True
    tombstones = tuple(home.staging_dir.glob("plugin-uninstall-*"))
    assert len(tombstones) == 1
    assert not (tombstones[0] / "plugin.toml").exists()
    assert (tombstones[0] / "hooks" / "audit.tool" / "hook.json").is_file()


def test_uninstall_explains_a_package_from_a_retired_generation(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    stale = home.plugins_dir / "vendor.bundle"
    stale.mkdir(parents=True)
    (stale / "plugin.yaml").write_text(
        "schema_version: 1\nname: vendor.bundle\nversion: 1.0.0\n"
        "description: Retired fixture.\nauthor: APM\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="delete .* by hand to drop it"):
        PluginInstaller(home).uninstall("vendor.bundle")

    assert (stale / "plugin.yaml").is_file()


def test_uninstall_refuses_a_replaced_plugin_directory(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)
    installer = PluginInstaller(home)
    installer.install(source)
    target = home.plugins_dir / "vendor.bundle"
    original = home.staging_dir / "original"
    target.rename(original)
    target.mkdir()
    (target / "attacker.txt").write_text("retain\n", encoding="utf-8")

    with pytest.raises(ValueError, match="plugin.yaml is required"):
        installer.uninstall("vendor.bundle")
    assert (target / "attacker.txt").read_text(encoding="utf-8") == "retain\n"


def test_catalog_rejects_symlinked_plugins_parent_and_member(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)
    PluginInstaller(home).install(source)

    member = home.plugins_dir / "vendor.bundle" / "skills" / "review"
    retained_member = home.plugins_dir / "vendor.bundle" / "skills" / "retained"
    member.rename(retained_member)
    member.symlink_to(retained_member, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links|regular directories"):
        PluginCatalog(home).resolve("vendor.bundle/review")

    member.unlink()
    retained_member.rename(member)
    retained_plugins = home.root / "retained-plugins"
    home.plugins_dir.rename(retained_plugins)
    home.plugins_dir.symlink_to(retained_plugins, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        PluginCatalog(home).list()


@pytest.mark.parametrize("swap", ("parent", "plugin", "kind"))
def test_catalog_with_open_member_rejects_parent_plugin_and_kind_swaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)
    PluginInstaller(home).install(source)
    catalog = PluginCatalog(home)
    installer = catalog._installer
    original = installer._verify_opened_plugin

    def replace_member(directory_fd: int, plugin_id: str, *, phase: str):
        plugin = original(directory_fd, plugin_id, phase=phase)
        if swap == "parent":
            retained = home.root / "retained-plugins"
            home.plugins_dir.rename(retained)
            home.plugins_dir.symlink_to(retained, target_is_directory=True)
        elif swap == "plugin":
            plugin_root = home.plugins_dir / plugin_id
            retained = home.plugins_dir / "verified-original"
            plugin_root.rename(retained)
            shutil.copytree(retained, plugin_root)
        else:
            skills = home.plugins_dir / plugin_id / "skills"
            retained = home.plugins_dir / plugin_id / "verified-skills"
            skills.rename(retained)
            shutil.copytree(retained, skills)
        return plugin

    monkeypatch.setattr(installer, "_verify_opened_plugin", replace_member)
    with pytest.raises(ValueError, match="changed"):
        catalog.with_open_member("vendor.bundle/review", lambda root, member: None)


def test_uninstall_keeps_the_initially_verified_descriptor_through_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "vendor.bundle"
    _plugin(source)
    installer = PluginInstaller(home)
    installer.install(source)
    descriptors: list[tuple[int, int, int, str]] = []
    original = installer._verify_opened_plugin

    def record_descriptor(directory_fd: int, plugin_id: str, *, phase: str):
        inode = os.fstat(directory_fd)
        descriptors.append((directory_fd, inode.st_dev, inode.st_ino, phase))
        return original(directory_fd, plugin_id, phase=phase)

    monkeypatch.setattr(installer, "_verify_opened_plugin", record_descriptor)
    assert installer.uninstall("vendor.bundle") is True
    assert [item[3] for item in descriptors] == ["installed", "quarantined"]
    assert descriptors[0][:3] == descriptors[1][:3]
