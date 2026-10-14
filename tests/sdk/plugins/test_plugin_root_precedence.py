"""同名插件在系统目录与用户目录各有一份时的优先级。

用户在本机从 AgentHub 装一个与随包同名的插件（例如 `spacemit-power`）时，生效的
应当是用户目录那份：技能、工具、Hook 都取用户的版本，系统那份被覆盖，而不是两份
并列出现在目录里。
"""
from __future__ import annotations

from pathlib import Path

from bianbu_agent.home import AgentHome
from bianbu_agent.plugins import PluginCatalog, PluginInstaller


PLUGIN_ID = "spacemit-power"


def _plugin(root: Path, *, version: str, skill: str) -> None:
    """One minimal Plugin package: a manifest plus a single Skill member."""

    skill_root = root / "skills" / skill
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: fixture skill {skill}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (root / "plugin.yaml").write_text(
        f"schema_version: 1\nname: {PLUGIN_ID}\nversion: {version}\ndescription: {skill}\n",
        encoding="utf-8",
    )


def test_user_plugin_directory_overrides_the_packaged_one(tmp_path, monkeypatch) -> None:
    packaged_root = tmp_path / "usr-share"
    _plugin(
        packaged_root / "plugins" / PLUGIN_ID,
        version="1.0.0",
        skill="packaged-skill",
    )
    monkeypatch.setenv("BIANBU_SYSTEM_RESOURCE_ROOT", str(packaged_root))

    home = AgentHome(tmp_path / "profile" / ".bianbu-agent").prepare()
    _plugin(home.plugins_dir / PLUGIN_ID, version="2.0.0", skill="user-skill")

    installer = PluginInstaller(home)
    plugins = installer.list()
    assert [plugin.manifest.id for plugin in plugins] == [PLUGIN_ID]
    assert plugins[0].manifest.version == "2.0.0"
    assert plugins[0].manifest.description == "user-skill"
    assert installer.package_root(PLUGIN_ID) == home.plugins_dir / PLUGIN_ID
    # 目录里只出现用户那份的成员：系统那份的 packaged-skill 不该同时可见。
    assert [
        member.qualified_id for member in PluginCatalog(home).list("skill")
    ] == [f"{PLUGIN_ID}/user-skill"]
