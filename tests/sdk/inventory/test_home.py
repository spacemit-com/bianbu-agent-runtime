from pathlib import Path

import pytest

from bianbu_agent.home import AgentHome
from bianbu_agent.inventory import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
)


def test_home_prepares_the_complete_project_local_layout(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    prepared = home.prepare()

    assert prepared is home
    assert home.root == (tmp_path / ".bianbu-agent").resolve()
    assert home.inventory_path == home.root / "inventory.sqlite3"
    assert home.plugin_dirs == (home.root / "plugins",)
    assert home.mcp_dirs == (home.root / "mcps",)
    assert home.skill_dirs == (home.root / "skills",)
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


def test_home_resolves_first_class_mcp_root(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()

    assert home.mcps_dir == home.root / "mcps"


def test_capability_source_normalizes_identity_and_freezes_provenance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tools" / "read_file"
    root.mkdir(parents=True)
    manifest = root / "tool.py"
    manifest.write_text("def create_tool(context): ...\n", encoding="utf-8")

    source = CapabilitySource(
        kind=CapabilityKind.TOOL,
        capability_id=" read_file ",
        version=" 1.0.0 ",
        root=root,
        manifest_path=manifest,
        content_digest="a" * 64,
        origin=CapabilityOrigin.STANDALONE,
        metadata={"channel": "local"},
    )

    assert source.capability_id == "read_file"
    assert source.version == "1.0.0"
    assert source.root == root.resolve()
    assert source.manifest_path == manifest.resolve()
    assert source.metadata == {"channel": "local"}
    with pytest.raises(TypeError):
        source.metadata["channel"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize("field", ["capability_id", "version", "content_digest"])
def test_capability_source_rejects_missing_identity(
    tmp_path: Path,
    field: str,
) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    manifest = root / "SKILL.md"
    manifest.write_text("---\nname: demo\n---\n", encoding="utf-8")
    values = {
        "kind": CapabilityKind.SKILL,
        "capability_id": "demo",
        "version": "1.0.0",
        "root": root,
        "manifest_path": manifest,
        "content_digest": "b" * 64,
        "origin": CapabilityOrigin.STANDALONE,
    }
    values[field] = ""

    with pytest.raises(ValueError, match=field):
        CapabilitySource(**values)  # type: ignore[arg-type]
