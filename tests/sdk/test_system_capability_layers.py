from pathlib import Path
import shutil

import pytest

from bianbu_agent.home import AgentHome
from bianbu_agent.plugins import PluginInstaller
from bianbu_agent.skills.manager import SkillPackageManager


@pytest.fixture
def layers(tmp_path, monkeypatch):
    system = tmp_path / 'system'
    system.mkdir()
    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(system))
    monkeypatch.setattr('bianbu_agent.mcp.system.SYSTEM_MCP_ROOT', system / 'mcps')
    return system, AgentHome(tmp_path / 'user').prepare()


def test_packaged_plugin_loads_without_copy_and_user_override_wins(layers):
    system, home = layers
    source = Path(__file__).resolve().parents[2] / 'resources/plugins/bianbu.audit'
    packaged = system / 'plugins/bianbu.audit'
    shutil.copytree(source, packaged)
    manager = PluginInstaller(home)
    assert manager.inspect('bianbu.audit').id == 'bianbu.audit'
    assert not (home.plugins_dir / 'bianbu.audit').exists()
    assert manager.with_open_member('bianbu.audit/audit.after_run', lambda path, _: (path / 'hook.json').is_file())
    manager.install(packaged)
    assert manager.package_root('bianbu.audit') == home.plugins_dir / 'bianbu.audit'
    with pytest.raises(ValueError, match='system Plugin'):
        manager.uninstall('bianbu.audit')
    assert not (system / '.capability.lock').exists()


def test_packaged_skill_reads_upgrade_directly_and_install_stays_user(layers, tmp_path):
    system, home = layers
    root = system / 'skills/example'
    root.mkdir(parents=True)
    manifest = root / 'SKILL.md'
    manifest.write_text('---\nname: example\ndescription: Example\nversion: 1.0.0\n---\nExample')
    manager = SkillPackageManager(home)
    assert manager.inspect('example').version == '1.0.0'
    manifest.write_text(manifest.read_text().replace('1.0.0', '2.0.0'))
    assert manager.inspect('example').version == '2.0.0'
    assert not (home.skills_dir / 'example').exists()
    manager.install(root)
    assert (home.skills_dir / 'example/SKILL.md').is_file()
    with pytest.raises(ValueError, match='system Skill'):
        manager.uninstall('example')
    assert not (system / '.capability.lock').exists()


def test_packaged_mcp_is_visible_to_manager_and_user_override_wins(layers):
    from bianbu_agent.mcp.manager import MCPPackageManager

    system, home = layers
    root = system / 'mcps/vendor.example'
    root.mkdir(parents=True)
    (root / 'mcp.json').write_text('{"transport":"stdio","command":"example"}')
    manager = MCPPackageManager(home)
    assert manager.inspect('vendor.example').root == root
    assert manager.list()[0].root == root
    manager.install(root)
    assert manager.inspect('vendor.example').root == home.mcps_dir / 'vendor.example'
    with pytest.raises(ValueError, match='system MCP'):
        manager.uninstall('vendor.example')
    assert not (system / '.capability.lock').exists()


def test_user_declared_skill_directory_needs_no_ownership_metadata(layers):
    """A Skill dropped into the user root is managed like an installed one.

    The Runtime discovers `skills/<name>/SKILL.md` from its own roots, so a
    directory the user copied in by hand is a live Skill even though the
    installer never wrote ownership metadata for it.  The management commands
    used to answer `null` and skip it, which made the CLI disagree with the
    Agent's own `skills_list`.
    """

    _system, home = layers
    root = home.skills_dir / "hand-dropped"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: hand-dropped\ndescription: Dropped by hand\n---\nBody"
    )
    manager = SkillPackageManager(home)

    records = {record.capability_id: record for record in manager.list()}
    assert "hand-dropped" in records
    record = manager.inspect("hand-dropped")
    assert record is not None
    assert record.capability_id == "hand-dropped"
    assert record.version == "0.0.0"
    assert record.origin == "standalone" and record.owner_plugin_id is None
    assert manager.verify("hand-dropped") is True
    assert manager.inspect("missing-skill") is None

    # Deriving the record must never write SDK metadata into the user's directory.
    assert not (root / ".bianbu-agent-sdk-ownership.json").exists()
    assert sorted(path.name for path in root.iterdir()) == ["SKILL.md"]

    assert manager.uninstall("hand-dropped") is True
    assert not root.exists()
    assert manager.inspect("hand-dropped") is None


def test_user_skill_root_ignores_directories_that_are_not_skills(layers):
    """Junk in the user root is skipped instead of breaking the Skill listing."""

    _system, home = layers
    (home.skills_dir / "not-a-skill").mkdir(parents=True)
    (home.skills_dir / "broken").mkdir()
    (home.skills_dir / "broken" / "SKILL.md").write_text("no frontmatter here")
    manager = SkillPackageManager(home)

    assert manager.list() == ()
    assert manager.inspect("not-a-skill") is None
    assert manager.inspect("broken") is None
    assert manager.verify("not-a-skill") is False
    assert manager.verify("broken") is False
    assert manager.uninstall("not-a-skill") is False
    assert (home.skills_dir / "not-a-skill").is_dir()
    assert (home.skills_dir / "broken").is_dir()


def test_declared_user_skill_never_impersonates_the_system_skill(layers):
    """A hand-dropped directory cannot claim a packaged Skill's identity."""

    system, home = layers
    packaged = system / "skills" / "example"
    packaged.mkdir(parents=True)
    (packaged / "SKILL.md").write_text(
        "---\nname: example\ndescription: Packaged\nversion: 1.0.0\n---\nPackaged"
    )
    shadow = home.skills_dir / "example"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text(
        "---\nname: example\ndescription: Hand dropped\nversion: 9.9.9\n---\nMine"
    )
    manager = SkillPackageManager(home)

    assert manager.inspect("example").version == "9.9.9"
    with pytest.raises(ValueError, match="system Skill"):
        manager.uninstall("example")
    assert shadow.is_dir()
