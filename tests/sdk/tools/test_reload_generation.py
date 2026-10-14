import pytest

from bianbu_agent.inventory import InventorySnapshot
from bianbu_agent.plugins import CapabilityReloadError, PluginManager
from bianbu_agent.agents import AgentPackageManager
from bianbu_agent.agents.projects import init_agent_project, build_agent_project
from bianbu_agent.home import AgentHome


@pytest.mark.asyncio
async def test_reload_tracks_bundled_agent_tool_install_and_uninstall(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / "home").prepare()
    packages = AgentPackageManager(home)
    project = init_agent_project(tmp_path / "hello", agent_id="hello", name="Hello")
    artifact = build_agent_project(project.root)
    manager = PluginManager(
        snapshot_provider=lambda: InventorySnapshot(revision=0),
        workspace_root=home.workspace_dir,
        plugin_data_root=home.workspace_dir / "plugin-data",
    )
    await manager.start()
    try:
        packages.install(artifact.path)
        assert (await manager.reload_installed()).changed is True
        assert "hello/greet" in manager.current_generation.tools
        assert packages.uninstall("hello") is True
        assert (await manager.reload_installed()).changed is True
        assert "hello/greet" not in manager.current_generation.tools
        assert (await manager.reload_installed()).changed is False
    finally:
        await manager.close()
