from pathlib import Path

from bianbu_agent import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    AgentHome,
)


def test_project_home_prepares_the_documented_project_local_layout(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")

    assert home.prepare() is home
    assert home.root == (tmp_path / ".bianbu-agent").resolve()
    assert home.skills_dir == home.root / "skills"
    assert home.plugins_dir == home.root / "plugins"
    assert home.mcps_dir == home.root / "mcps"
    assert home.agents_dir == home.root / "agents"
    assert home.models_dir == home.root / "models"
    assert home.sessions_dir == home.root / "sessions"
    assert home.workspace_dir == home.root / "workspace"
    assert home.logs_dir == home.root / "logs"
    # Nothing writes the cache root any more, so a plain start leaves it out.
    assert home.cache_dir == home.root / "cache"
    assert not home.cache_dir.exists()
    assert set(home.required_directories()) == {
        home.root,
        home.agents_dir,
        home.models_dir,
        home.sessions_dir,
        home.workspace_dir,
        home.logs_dir,
        home.staging_dir,
    }
    assert all(path.is_dir() for path in home.required_directories())
    # Extension roots grow on first use, so a plain start leaves no empty junk.
    assert not any(path.exists() for path in home.plugin_dirs)
    assert not any(path.exists() for path in home.mcp_dirs)
    assert not any(path.exists() for path in home.skill_dirs)
    assert home.ensure(home.skill_dirs[0]).is_dir()
    assert not (home.root / "hooks").exists()
    assert not (home.root / "runs").exists()
    assert not (home.root / "session-events").exists()


def test_root_capability_source_retains_the_root_capability_kind(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    root = tmp_path / "tool"
    root.mkdir()
    source_file = root / "tool.py"
    source_file.write_text("# Tool source\n", encoding="utf-8")

    source = CapabilitySource(
        kind=CapabilityKind.TOOL,
        capability_id="demo",
        version="1.0.0",
        root=root,
        manifest_path=source_file,
        content_digest="d" * 64,
        origin=CapabilityOrigin.STANDALONE,
    )

    assert source.kind is CapabilityKind.TOOL
    assert source.root == root
