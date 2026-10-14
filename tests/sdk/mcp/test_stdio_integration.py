from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys
import time

import psutil
import pytest
from mcp.shared.exceptions import McpError

from bianbu_agent.mcp import MCPClient, MCPServerConfig


@pytest.mark.asyncio
async def test_real_stdio_server_discovers_and_calls_echo_tool() -> None:
    server = Path(__file__).parent / "fixtures" / "stdio_server.py"
    client = MCPClient(
        MCPServerConfig(
            transport="stdio",
            command=sys.executable,
            args=(str(server),),
            timeout_seconds=10,
        )
    )

    tools = await client.list_tools()
    result = await client.call_tool("echo", {"message": "MCP_STDIO_OK"})

    assert len(tools) == 1
    assert tools[0].name == "echo"
    assert tools[0].description == "Return the supplied message."
    assert tools[0].input_schema == {
        "properties": {"message": {"title": "Message", "type": "string"}},
        "required": ["message"],
        "title": "echoArguments",
        "type": "object",
    }
    assert result.content == '{"result":"MCP_STDIO_OK"}'
    assert result.structured_content == {"result": "MCP_STDIO_OK"}
    assert result.is_error is False


def _dying_client(**overrides) -> MCPClient:
    server = Path(__file__).parent / "fixtures" / "stdio_dying_server.py"
    return MCPClient(
        MCPServerConfig(
            transport="stdio",
            command=sys.executable,
            args=(str(server),),
            timeout_seconds=10,
        ),
        **overrides,
    )


def _child_pids() -> set[int]:
    return {child.pid for child in psutil.Process(os.getpid()).children(recursive=True)}


async def _wait_for_child_restart(previous: set[int], *, timeout: float = 10.0) -> set[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _child_pids()
        if current - previous:
            return current
        await asyncio.sleep(0.02)
    raise AssertionError("stdio MCP child was never restarted")


@pytest.mark.asyncio
async def test_recovers_after_a_stdio_child_is_killed_without_a_call() -> None:
    client = _dying_client(keepalive_seconds=0.05)
    try:
        await client.start()
        children = _child_pids()
        for pid in children:
            os.kill(pid, signal.SIGKILL)

        await _wait_for_child_restart(children)

        result = await client.call_tool("echo", {"message": "NEW_CHILD"})
        assert result.structured_content == {"result": "NEW_CHILD"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_does_not_replay_a_call_the_dying_server_already_received(tmp_path) -> None:
    client = _dying_client(keepalive_seconds=0.05)
    try:
        await client.start()
        children = _child_pids()
        ran = tmp_path / "exit-process-ran.txt"

        with pytest.raises(McpError):
            await client.call_tool("exit_process", {"count_file": str(ran)})

        await _wait_for_child_restart(children)
        result = await client.call_tool("echo", {"message": "NEXT_CALL"})
    finally:
        await client.close()

    assert ran.read_text(encoding="utf-8") == "ran\n"
    assert result.structured_content == {"result": "NEXT_CALL"}
