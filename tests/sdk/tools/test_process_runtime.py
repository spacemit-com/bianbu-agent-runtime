from __future__ import annotations

from pathlib import Path

import pytest

import bianbu_agent.tools as sdk_tools
from bianbu_agent.tools import ProcessManager, ProcessRequest


def test_sdk_tools_reexports_the_langchain_tool_decorator() -> None:
    from langchain_core.tools import tool as langchain_tool

    assert sdk_tools.tool is langchain_tool
    assert "tool" in sdk_tools.__all__


def test_process_runtime_is_a_public_tool_agnostic_service() -> None:
    assert hasattr(sdk_tools, "ProcessManager")
    assert hasattr(sdk_tools, "ProcessRequest")


@pytest.mark.asyncio
async def test_process_manager_runs_a_foreground_command_in_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = ProcessManager(workspace=workspace)

    result = await manager.run(
        ProcessRequest(
            command="pwd; printf tool-runtime",
            timeout_seconds=10,
        )
    )

    assert result.exit_code == 0
    assert result.output == f"{workspace}\ntool-runtime"
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_process_manager_tracks_a_background_process_until_wait(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = ProcessManager(workspace=workspace)

    handle = await manager.start(
        ProcessRequest(command="printf background-result", timeout_seconds=10)
    )
    result = await manager.wait(handle.session_id, timeout_seconds=10)

    assert handle.session_id.startswith("process-")
    assert result.output == "background-result"
    assert result.exit_code == 0
    assert await manager.list() == []


@pytest.mark.asyncio
async def test_process_manager_writes_to_and_terminates_background_processes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = ProcessManager(workspace=workspace)
    interactive = await manager.start(
        ProcessRequest(command="read line; printf got:$line", timeout_seconds=10)
    )

    await manager.write(interactive.session_id, "hello\n")
    completed = await manager.wait(interactive.session_id, timeout_seconds=10)

    assert completed.output == "got:hello"
    sleeping = await manager.start(ProcessRequest(command="sleep 30"))
    killed = await manager.kill(sleeping.session_id)
    assert killed.exit_code != 0
