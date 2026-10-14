from __future__ import annotations

from pathlib import Path

from bianbu_agent.home import AgentHome
from bianbu_agent.plugins import PluginPackageManager
from bianbu_agent.skills import SkillPackageManager


def _tool(root: Path, name: str) -> Path:
    """Build one Tool directory owned by a Plugin package."""
    source = root / "tool-sources" / name
    source.mkdir(parents=True)
    (source / "tool.py").write_text(
        "from bianbu_agent.tools import tool\n"
        "def create_tool(context):\n"
        "    @tool\n"
        f"    def {name}() -> str:\n"
        "        '''Return a test value.'''\n"
        "        return 'ok'\n"
        f"    return {name}\n",
        encoding="utf-8",
    )
    return source


def _skill(root: Path, name: str) -> Path:
    source = root / f"source-skill-{name}"
    source.mkdir()
    (source / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\nversion: 1.0.0\n---\n\nUse it.\n",
        encoding="utf-8",
    )
    return source


def test_standalone_skill_and_plugin_package_managers_survive_restart(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()

    skills = SkillPackageManager(home)
    assert skills.install(_skill(tmp_path, "writer")).capability_id == "writer"

    assert [item.capability_id for item in SkillPackageManager(home).list()] == ["writer"]
    assert SkillPackageManager(home).verify("writer")


def test_plugin_package_manager_keeps_tools_in_namespaced_package(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    source = tmp_path / "office.core"
    source.mkdir()
    (source / "tools").mkdir()
    _tool(source, "clock").rename(source / "tools" / "clock")
    (source / "tool-sources").rmdir()
    (source / "plugin.yaml").write_text(
        """\
schema_version: 1
name: office.core
version: 1.0.0
description: Package manager fixture.
""",
        encoding="utf-8",
    )

    manager = PluginPackageManager(home)
    assert manager.install(source).id == "office.core"

    restarted = PluginPackageManager(home)
    assert [item.id for item in restarted.list()] == ["office.core"]
    member = restarted.resolve("office.core/clock")
    assert member is not None
    assert member.kind == "tool"
    assert restarted.resolve("clock") is None
    assert not (home.root / "tools").exists()
    assert (home.plugins_dir / "office.core" / "tools" / "clock").exists()


def test_skill_package_manager_excludes_plugin_contributed_skills(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    source = tmp_path / "office.core"
    skill = source / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review fixture\nversion: 1.0.0\n---\nReview.\n",
        encoding="utf-8",
    )
    (source / "plugin.yaml").write_text(
        "schema_version: 1\nname: office.core\nversion: 1.0.0\ndescription: Fixture.\n",
        encoding="utf-8",
    )
    PluginPackageManager(home).install(source)

    listed = SkillPackageManager(home).list()
    assert listed == ()
