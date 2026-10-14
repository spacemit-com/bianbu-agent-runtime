from __future__ import annotations

from pathlib import Path

import pytest

from bianbu_agent.home import AgentHome
from bianbu_agent.mcp import install as install_module
from bianbu_agent.mcp.config import MCPServerConfig
from bianbu_agent.mcp.install import (
    discard_prepared_managed_mcp,
    pin_npm_dependency,
    prepare_managed_mcp,
)
from bianbu_agent.mcp.managed import parse_managed_mcp
from bianbu_agent.mcp.readiness import MCP_READY, MCP_UNVERIFIED, MCPProbeResult
from bianbu_agent.mcp.vendor import VendoredNpmServer


def _home(tmp_path: Path) -> AgentHome:
    return AgentHome(tmp_path / "data").prepare()


def _stub_vendor(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, Path]],
) -> None:
    def vendor(spec: str, target: Path) -> VendoredNpmServer:
        calls.append((spec, target))
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.js").write_text("// stub\n", encoding="utf-8")
        return VendoredNpmServer(
            package=spec,
            root=target,
            script=target / "index.js",
            interpreter=Path("/usr/bin/node"),
        )

    monkeypatch.setattr(install_module, "vendor_npm_server", vendor)


def test_pin_npm_dependency_replaces_only_the_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path)
    definition = parse_managed_mcp("Bilibili", "npx -y bilibili-mcp-js --port 1")
    calls: list[tuple[str, Path]] = []
    _stub_vendor(monkeypatch, calls)

    pinned, root = pin_npm_dependency(definition, home)

    assert calls == [("bilibili-mcp-js", home.mcp_runtime_dir / "mcp.bilibili")]
    assert root == home.mcp_runtime_dir / "mcp.bilibili"
    assert pinned.executable == "/usr/bin/node"
    assert pinned.args == (
        str(home.mcp_runtime_dir / "mcp.bilibili" / "index.js"),
        "--port",
        "1",
    )
    assert pinned.command_line.startswith("/usr/bin/node ")
    # The declaration itself is unchanged; only how it starts is.
    assert pinned.npm_package == "bilibili-mcp-js"
    assert pinned.display_name == "Bilibili"


def test_pin_npm_dependency_leaves_declarations_we_cannot_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path)
    monkeypatch.setattr(
        install_module,
        "vendor_npm_server",
        lambda *_args, **_kwargs: pytest.fail("must not vendor"),
    )

    definition = parse_managed_mcp("Weather", "weather-mcp --flag")
    pinned, root = pin_npm_dependency(definition, home)

    assert pinned is definition
    assert root is None
    assert not home.mcp_runtime_dir.exists()


@pytest.mark.asyncio
async def test_prepare_probes_the_document_that_will_be_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path)
    definition = parse_managed_mcp("Bilibili", "npx bilibili-mcp-js")
    calls: list[tuple[str, Path]] = []
    _stub_vendor(monkeypatch, calls)
    probed: list[MCPServerConfig] = []

    async def probe(*, workspace, config):
        probed.append(config)
        return MCPProbeResult(MCP_READY, ("echo",), "")

    monkeypatch.setattr(install_module, "probe_mcp_server", probe)

    prepared = await prepare_managed_mcp(definition, home)

    assert [config.command for config in probed] == ["/usr/bin/node"]
    assert probed[0].args == (str(home.mcp_runtime_dir / "mcp.bilibili" / "index.js"),)
    assert probed[0].startup_timeout_seconds == 120.0
    assert prepared.probe.ready
    assert prepared.definition.command_line == (
        f"/usr/bin/node {home.mcp_runtime_dir / 'mcp.bilibili' / 'index.js'}"
    )


@pytest.mark.asyncio
async def test_prepare_without_verify_installs_without_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path)
    definition = parse_managed_mcp("Bilibili", "npx bilibili-mcp-js")
    _stub_vendor(monkeypatch, [])

    async def probe(**_kwargs):
        raise AssertionError("--no-verify must not start the server")

    monkeypatch.setattr(install_module, "probe_mcp_server", probe)

    prepared = await prepare_managed_mcp(definition, home, verify=False)

    assert prepared.probe.status == MCP_UNVERIFIED
    assert prepared.probe.tools == ()
    assert not prepared.probe.ready
    assert not prepared.probe.verified
    assert prepared.vendor_root == home.mcp_runtime_dir / "mcp.bilibili"


def test_discard_removes_the_vendored_tree_and_keeps_other_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path)
    definition = parse_managed_mcp("Bilibili", "npx bilibili-mcp-js")
    _stub_vendor(monkeypatch, [])
    prepared = _prepared_placeholder(definition, home)
    keep = home.mcp_runtime_dir / "mcp.other"
    keep.mkdir(parents=True)

    discard_prepared_managed_mcp(prepared)

    assert not (home.mcp_runtime_dir / "mcp.bilibili").exists()
    assert keep.exists()


def _prepared_placeholder(definition, home):
    from bianbu_agent.mcp.install import PreparedManagedMCP

    target = home.mcp_runtime_dir / "mcp.bilibili"
    target.mkdir(parents=True, exist_ok=True)
    return PreparedManagedMCP(
        definition,
        target,
        MCPProbeResult(MCP_UNVERIFIED, (), ""),
    )
