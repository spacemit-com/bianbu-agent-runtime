from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from bianbu_agent.mcp.config import MCPServerConfig
from bianbu_agent.mcp.readiness import (
    MCP_READY,
    MCP_UNHEALTHY,
    failure_detail,
    probe_mcp_server,
)

FIXTURE = Path(__file__).parent / "fixtures" / "stdio_server.py"


def _fixture_config(**overrides: object) -> MCPServerConfig:
    values: dict[str, object] = {
        "transport": "stdio",
        "command": sys.executable,
        "args": (str(FIXTURE),),
        "timeout_seconds": 20.0,
    }
    values.update(overrides)
    return MCPServerConfig(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_probe_lists_the_tools_of_a_server_that_answers(
    tmp_path: Path,
) -> None:
    result = await probe_mcp_server(
        workspace=tmp_path,
        config=_fixture_config(),
    )

    assert result.status == MCP_READY
    assert result.tools == ("echo",)
    assert result.detail == ""
    assert result.ready
    assert result.verified


@pytest.mark.asyncio
async def test_probe_names_the_error_of_a_server_that_cannot_start(
    tmp_path: Path,
) -> None:
    result = await probe_mcp_server(
        workspace=tmp_path,
        config=MCPServerConfig(
            transport="stdio",
            command="/nonexistent/managed-mcp",
            timeout_seconds=20.0,
        ),
    )

    assert result.status == MCP_UNHEALTHY
    assert result.tools == ()
    assert "FileNotFoundError" in result.detail


@pytest.mark.asyncio
async def test_probe_reports_a_server_that_never_answers_the_handshake(
    tmp_path: Path,
) -> None:
    result = await probe_mcp_server(
        workspace=tmp_path,
        config=MCPServerConfig(
            transport="stdio",
            command=sys.executable,
            args=("-c", "import time; time.sleep(30)"),
            timeout_seconds=30.0,
            startup_timeout_seconds=2.0,
        ),
    )

    assert result.status == MCP_UNHEALTHY
    assert result.detail == "MCP server did not answer the MCP handshake within 2s"


@pytest.mark.asyncio
async def test_probe_reads_a_declaration_from_disk_when_given_a_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mcp.broken"
    root.mkdir()
    (root / "mcp.json").write_text(
        '{"transport": "stdio", "command": "/nonexistent/managed-mcp"}',
        encoding="utf-8",
    )

    result = await probe_mcp_server(workspace=tmp_path, root=root)

    assert result.status == MCP_UNHEALTHY
    assert "FileNotFoundError" in result.detail


@pytest.mark.asyncio
async def test_probe_reports_a_declaration_that_cannot_be_parsed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mcp.invalid"
    root.mkdir()
    (root / "mcp.json").write_text('{"transport": "carrier-pigeon"}', encoding="utf-8")

    result = await probe_mcp_server(workspace=tmp_path, root=root)

    assert result.status == MCP_UNHEALTHY
    assert result.detail.startswith("ValueError: transport must be")


@pytest.mark.asyncio
async def test_probe_requires_exactly_one_declaration_source(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="exactly one of root or config"):
        await probe_mcp_server(workspace=tmp_path)
    with pytest.raises(ValueError, match="exactly one of root or config"):
        await probe_mcp_server(
            workspace=tmp_path,
            root=tmp_path,
            config=_fixture_config(),
        )


def test_failure_detail_unwraps_a_grouped_transport_error() -> None:
    inner = FileNotFoundError("no such file or directory: 'npx'")
    grouped = ExceptionGroup("unhandled errors in a TaskGroup", [inner])

    assert failure_detail(grouped) == "FileNotFoundError: no such file or directory: 'npx'"


def test_failure_detail_explains_a_startup_timeout() -> None:
    grouped = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [asyncio.TimeoutError()],
    )

    assert failure_detail(grouped, startup_timeout=120.0) == (
        "MCP server did not answer the MCP handshake within 120s"
    )


def test_failure_detail_still_names_a_bare_exception() -> None:
    assert failure_detail(RuntimeError()) == "RuntimeError while starting the MCP server"
