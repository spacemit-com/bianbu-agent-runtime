from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bianbu_agent.home import AgentHome
from bianbu_agent.mcp.config import MCPServerConfig
from bianbu_agent.mcp.managed import ManagedMCPDefinition
from bianbu_agent.mcp.lifecycle import MCP_PHASE_INSTALLING, MCP_PHASE_STARTING
from bianbu_agent.mcp.preparation import (
    MCP_PREPARATION_FAILED,
    MCP_PREPARED,
    MCP_PREPARING,
    MCPPreparationManager,
)
from bianbu_agent.mcp.readiness import MCP_READY, MCPProbeResult


def _home(tmp_path: Path) -> AgentHome:
    return AgentHome(tmp_path / "data").prepare()


def _npx_config() -> MCPServerConfig:
    return MCPServerConfig(
        transport="stdio",
        display_name="Bilibili",
        command="npx",
        args=("-y", "bilibili-mcp-js"),
        startup_timeout_seconds=120.0,
    )


async def _until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the expected state was not reached")


def _manager(home: AgentHome, **kwargs) -> tuple[MCPPreparationManager, list[int]]:
    reloads: list[int] = []

    async def reload():
        reloads.append(1)

    manager = MCPPreparationManager(
        home,
        reload=reload,
        reload_debounce_seconds=0.0,
        **kwargs,
    )
    return manager, reloads


@pytest.mark.asyncio
async def test_a_declaration_without_a_package_is_probed_once_and_remembered(
    tmp_path: Path,
) -> None:
    """The tools it lists are what a UI shows before an Expert selects it."""

    home = _home(tmp_path)
    config = MCPServerConfig(transport="stdio", command="/bin/echo")
    probed: list[str] = []

    async def probe(server: MCPServerConfig) -> MCPProbeResult:
        probed.append(server.command)
        return MCPProbeResult(status=MCP_READY, tools=("weather_now",), detail="")

    manager, reloads = _manager(home, probe=probe)
    try:
        # It can already start, so it stays connectable while the probe runs:
        # a Generation is free to connect it in the meantime.
        state = manager.ensure("mcp.weather", config)
        assert state.status == MCP_PREPARED
        assert manager.config_for("mcp.weather", config).command == "/bin/echo"
        await _until(lambda: bool(manager.state("mcp.weather").tools))
        prepared = manager.state("mcp.weather")
        assert prepared.tools == ("weather_now",)
        assert prepared.checked_at is not None
        assert probed == ["/bin/echo"]
        # Readiness never changed, so no Generation had to be rebuilt.
        assert reloads == []
    finally:
        await manager.close()

    # A later Worker answers from the record instead of starting the server
    # again: the tools it proved stay visible without a connection.
    follower, follower_reloads = _manager(
        home,
        probe=lambda _config: pytest.fail("a recorded server must not start again"),
    )
    try:
        state = follower.ensure("mcp.weather", config)
        assert state.status == MCP_PREPARED
        assert state.tools == ("weather_now",)
        assert state.checked_at is not None
        await asyncio.sleep(0)
        assert follower_reloads == []
    finally:
        await follower.close()


@pytest.mark.asyncio
async def test_the_caller_that_connects_the_server_is_not_probed(
    tmp_path: Path,
) -> None:
    """A second instance would fight the one the Generation is about to start."""

    home = _home(tmp_path)
    manager, reloads = _manager(
        home,
        probe=lambda _config: pytest.fail("must not start a second instance"),
    )
    try:
        state = manager.ensure(
            "mcp.weather",
            MCPServerConfig(transport="stdio", command="/bin/echo"),
            probe=False,
        )
        assert state.status == MCP_PREPARED
        assert state.launchable is True
        assert state.tools == ()
        await asyncio.sleep(0)
        assert reloads == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_failed_probe_still_leaves_the_server_connectable(
    tmp_path: Path,
) -> None:
    """A probe that could not start it once is a verdict, not a lock.

    The Generation that tries the same declaration reports its own, better
    informed result; dropping it would cost the tools of a server that works.
    """

    home = _home(tmp_path)

    async def probe(_config: MCPServerConfig) -> MCPProbeResult:
        raise RuntimeError("connection closed")

    manager, reloads = _manager(home, probe=probe)
    try:
        state = manager.ensure(
            "mcp.weather",
            MCPServerConfig(transport="stdio", command="/bin/echo"),
        )
        assert state.ready is True
        await _until(
            lambda: manager.state("mcp.weather").status == MCP_PREPARATION_FAILED
        )
        failed = manager.state("mcp.weather")
        assert failed.launchable is True
        assert "connection closed" in failed.detail
        # Readiness never dropped, so no Generation had to be rebuilt.
        assert reloads == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_missing_package_is_installed_and_probed_in_the_background(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    prepared: list[str] = []

    def prepare(definition: ManagedMCPDefinition) -> tuple[ManagedMCPDefinition, Path]:
        prepared.append(definition.npm_package)
        target = home.mcp_runtime_dir / definition.plugin_id
        return (
            ManagedMCPDefinition(
                plugin_id=definition.plugin_id,
                display_name=definition.display_name,
                executable="/usr/bin/node",
                args=(str(target / "index.js"),),
                transport="stdio",
                npm_package=definition.npm_package,
                npm_arguments=definition.npm_arguments,
            ),
            target,
        )

    probed: list[MCPServerConfig] = []

    async def probe(config: MCPServerConfig) -> MCPProbeResult:
        probed.append(config)
        return MCPProbeResult(MCP_READY, ("search",), "")

    manager, reloads = _manager(home, prepare=prepare, probe=probe)
    try:
        state = manager.ensure("mcp.bilibili", _npx_config())
        assert state.status == MCP_PREPARING
        await _until(lambda: (manager.state("mcp.bilibili") or state).ready)
        settled = manager.state("mcp.bilibili")
        assert settled is not None and settled.status == MCP_PREPARED
        assert settled.tools == ("search",)
        assert settled.vendored is True
        assert prepared == ["bilibili-mcp-js"]
        # The probe runs the pinned launcher, not npx.
        assert probed[0].command == "/usr/bin/node"
        assert probed[0].args == (str(home.mcp_runtime_dir / "mcp.bilibili" / "index.js"),)
        # The Generation that follows starts the local installation.
        assert manager.config_for("mcp.bilibili", _npx_config()).command == "/usr/bin/node"
        await _until(lambda: reloads == [1])
        # One server settles once: a second Generation is not asked for.
        await asyncio.sleep(0.05)
        assert reloads == [1]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_preparation_reports_the_phase_it_is_in(tmp_path: Path) -> None:
    """A multi-minute preparation has to say what it is waiting on.

    "preparing" alone is what an operator stares at while a 116MB npm install
    runs; the phase separates that from the handshake that follows it.
    """

    home = _home(tmp_path)
    release = asyncio.Event()

    def prepare(definition: ManagedMCPDefinition) -> tuple[ManagedMCPDefinition, Path]:
        target = home.mcp_runtime_dir / definition.plugin_id
        return (
            ManagedMCPDefinition(
                plugin_id=definition.plugin_id,
                display_name=definition.display_name,
                executable="/usr/bin/node",
                args=(str(target / "index.js"),),
                transport="stdio",
                npm_package=definition.npm_package,
                npm_arguments=definition.npm_arguments,
            ),
            target,
        )

    async def probe(_config: MCPServerConfig) -> MCPProbeResult:
        await release.wait()
        return MCPProbeResult(MCP_READY, ("search",), "")

    manager, _reloads = _manager(home, prepare=prepare, probe=probe)
    try:
        state = manager.ensure("mcp.bilibili", _npx_config())
        assert state.phase == MCP_PHASE_INSTALLING
        # The install is a real await point: the probe cannot have run yet.
        await _until(
            lambda: (manager.state("mcp.bilibili") or state).phase == MCP_PHASE_STARTING
        )
        release.set()
        await _until(lambda: (manager.state("mcp.bilibili") or state).ready)
        settled = manager.state("mcp.bilibili")
        assert settled is not None
        assert settled.status == MCP_PREPARED
        # ``phase`` describes work in flight only: a settled server carries no
        # stale "installing" for a reader to misread.
        assert settled.phase == ""
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_failed_installation_is_reported_once(tmp_path: Path) -> None:
    home = _home(tmp_path)

    def prepare(_definition: ManagedMCPDefinition):
        raise RuntimeError("npm install 'bilibili-mcp-js' failed (exit 1): offline")

    manager, reloads = _manager(home, prepare=prepare)
    try:
        assert manager.ensure("mcp.bilibili", _npx_config()).status == MCP_PREPARING
        await _until(
            lambda: (manager.state("mcp.bilibili") or
                     MCPProbeResult("x", (), "")).status == MCP_PREPARATION_FAILED
        )
        state = manager.state("mcp.bilibili")
        assert state is not None
        assert "offline" in state.detail
        # The failure still has to reach the Console, so it reloads once.
        await _until(lambda: reloads == [1])
        assert manager.ensure("mcp.bilibili", _npx_config()) is state
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_vendored_package_is_resolved_without_npm(tmp_path: Path) -> None:
    """Every later Worker start must not redo the npm work, only prove it once."""

    home = _home(tmp_path)
    installed = home.mcp_runtime_dir / "mcp.weather"
    (installed / "node_modules" / "weather-mcp").mkdir(parents=True)
    (installed / "node_modules" / "weather-mcp" / "package.json").write_text(
        '{"name": "weather-mcp", "bin": "index.js"}\n', encoding="utf-8"
    )
    (installed / "node_modules" / "weather-mcp" / "index.js").write_text(
        "//\n", encoding="utf-8"
    )
    async def probe(_config: MCPServerConfig) -> MCPProbeResult:
        return MCPProbeResult(status=MCP_READY, tools=("weather",), detail="")

    manager, reloads = _manager(
        home,
        prepare=lambda _definition: pytest.fail("must not install again"),
        probe=probe,
    )
    try:
        config = MCPServerConfig(
            transport="stdio", command="npx", args=("weather-mcp",)
        )
        state = manager.ensure("mcp.weather", config)
        # Nothing left to download and the launcher is local: it is ready now.
        assert state.status == MCP_PREPARED
        resolved = manager.config_for("mcp.weather", config)
        assert resolved.command != "npx"
        assert resolved.args == (
            str(installed / "node_modules" / "weather-mcp" / "index.js"),
        )
        await _until(lambda: bool(manager.state("mcp.weather").tools))
        assert manager.state("mcp.weather").tools == ("weather",)
        assert reloads == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_declaration_removed_while_downloading_leaves_no_tree(
    tmp_path: Path,
) -> None:
    """Uninstalling during the download must not leave 116MB nothing owns."""

    import threading

    home = _home(tmp_path)
    release = threading.Event()
    target = home.mcp_runtime_dir / "mcp.bilibili"

    def prepare(definition: ManagedMCPDefinition):
        release.wait(timeout=5)
        target.mkdir(parents=True, exist_ok=True)
        return (
            ManagedMCPDefinition(
                plugin_id=definition.plugin_id,
                display_name=definition.display_name,
                executable="/usr/bin/node",
                args=(str(target / "index.js"),),
                transport="stdio",
                npm_package=definition.npm_package,
            ),
            target,
        )

    manager, _reloads = _manager(
        home,
        prepare=prepare,
        probe=lambda _config: pytest.fail("a removed server must not be probed"),
    )
    source = tmp_path / "mcps" / "mcp.bilibili" / "mcp.json"
    source.parent.mkdir(parents=True)
    source.write_text("{}", encoding="utf-8")
    try:
        assert manager.ensure(
            "mcp.bilibili", _npx_config(), source=source
        ).status == MCP_PREPARING
        source.unlink()
        release.set()
        await _until(lambda: manager.state("mcp.bilibili") is None)
        assert not target.exists()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_an_installed_launcher_that_disappeared_is_resolved_again(
    tmp_path: Path,
) -> None:
    """An uninstall removes the tree; the next start must not run a path gone."""

    import shutil

    home = _home(tmp_path)
    installed = home.mcp_runtime_dir / "mcp.weather"
    (installed / "node_modules" / "weather-mcp").mkdir(parents=True)
    (installed / "node_modules" / "weather-mcp" / "package.json").write_text(
        '{"name": "weather-mcp", "bin": "index.js"}\n', encoding="utf-8"
    )
    (installed / "node_modules" / "weather-mcp" / "index.js").write_text(
        "//\n", encoding="utf-8"
    )
    prepared: list[ManagedMCPDefinition] = []
    launched: list[str] = []

    def prepare(definition: ManagedMCPDefinition):
        prepared.append(definition)
        return definition, None

    async def probe(server: MCPServerConfig) -> MCPProbeResult:
        launched.append(server.args[0] if server.args else server.command)
        return MCPProbeResult(status=MCP_READY, tools=(), detail="")

    manager, _reloads = _manager(
        home,
        prepare=prepare,
        probe=probe,
    )
    config = MCPServerConfig(transport="stdio", command="npx", args=("weather-mcp",))
    try:
        manager.ensure("mcp.weather", config)
        await _until(lambda: bool(launched))
        assert manager.config_for("mcp.weather", config).command != "npx"
        assert prepared == []
        assert launched == [str(installed / "node_modules" / "weather-mcp" / "index.js")]

        shutil.rmtree(installed)

        state = manager.ensure("mcp.weather", config)
        # The record described an installation that is gone with the tree, so
        # the declaration is installed again instead of answering "ready".
        assert state.status == MCP_PREPARING
        assert state.phase == MCP_PHASE_INSTALLING
        await _until(lambda: not manager.state("mcp.weather").preparing)
        assert prepared == [ManagedMCPDefinition(
            plugin_id="mcp.weather",
            display_name="mcp.weather",
            executable="npx",
            args=("weather-mcp",),
            npm_package="weather-mcp",
        )]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_an_explicit_refresh_retries_a_failed_server(tmp_path: Path) -> None:
    home = _home(tmp_path)
    attempts: list[int] = []

    def prepare(definition: ManagedMCPDefinition):
        attempts.append(1)
        raise RuntimeError("still offline")

    manager, _reloads = _manager(home, prepare=prepare)
    try:
        manager.ensure("mcp.bilibili", _npx_config())
        await _until(lambda: len(attempts) == 1)
        await _until(
            lambda: (manager.state("mcp.bilibili") or
                     MCPServerConfig(transport="stdio")).status
            == MCP_PREPARATION_FAILED
        )
        manager.invalidate_all()
        assert manager.ensure("mcp.bilibili", _npx_config()).status == MCP_PREPARING
        await _until(lambda: len(attempts) == 2)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_close_cancels_work_in_flight(tmp_path: Path) -> None:
    home = _home(tmp_path)
    started = asyncio.Event()

    def prepare(_definition: ManagedMCPDefinition):
        return (
            ManagedMCPDefinition(
                plugin_id="mcp.bilibili",
                display_name="Bilibili",
                executable="/bin/echo",
            ),
            None,
        )

    async def probe(_config: MCPServerConfig) -> MCPProbeResult:
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("cancelled")

    manager, _reloads = _manager(home, prepare=prepare, probe=probe)
    manager.ensure("mcp.bilibili", _npx_config())
    await asyncio.wait_for(started.wait(), timeout=5)
    await manager.close()
    assert manager.state("mcp.bilibili") is not None
