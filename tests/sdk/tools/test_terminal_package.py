from __future__ import annotations

import json
import asyncio
from pathlib import Path
import os
import time

import pytest

from bianbu_agent.tools.builtins import load_builtin, builtin_source

from bianbu_agent import ToolCallContext
from bianbu_agent.tools import DirectoryToolLoader, ToolLoadContext, ToolRuntimeFactory


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_terminal_executes_without_the_upstream_registry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    factory = ToolRuntimeFactory(workspace_root=workspace)
    root = "terminal"
    contribution = load_builtin(
        root,
        context=ToolLoadContext(
            workspace_root=workspace,
            services={"runtime_factory": factory},
        ),
    )

    result = await contribution.handler(
        ToolCallContext("run", "session", "agent"),
        {"command": "pwd; printf package-owned"},
    )

    assert result.content == (
        '{"output": "' + str(workspace) + '\\npackage-owned", '
        '"exit_code": 0, "status": "completed"}'
    )


@pytest.mark.asyncio
async def test_terminal_starts_a_managed_background_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    factory = ToolRuntimeFactory(workspace_root=workspace)
    root = "terminal"
    contribution = load_builtin(
        root,
        context=ToolLoadContext(
            workspace_root=workspace,
            services={"runtime_factory": factory},
        ),
    )

    session_id = ""
    try:
        result = await contribution.handler(
            ToolCallContext("run", "session", "agent"),
            {"command": "sleep 30", "background": True},
        )
        payload = __import__("json").loads(result.content)
        assert payload["status"] == "running"
        session_id = payload["session_id"]
        assert session_id
    finally:
        if session_id:
            metadata = json.loads(
                (
                    tmp_path / "runtime/tool-state/processes" / f"{session_id}.json"
                ).read_text(encoding="utf-8")
            )
            try:
                os.killpg(int(metadata["pid"]), 15)
            except ProcessLookupError:
                pass
        await factory.close()


@pytest.mark.asyncio
async def test_terminal_timeout_terminates_the_entire_process_group(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    factory = ToolRuntimeFactory(workspace_root=workspace)
    contribution = load_builtin(
        "terminal",
        context=ToolLoadContext(
            workspace_root=workspace,
            services={"runtime_factory": factory},
        ),
    )
    started_at = time.monotonic()

    result = await contribution.handler(
        ToolCallContext("run", "session", "agent"),
        {"command": "sleep 2 & echo $! > child.pid; wait", "timeout": 1},
    )
    elapsed = time.monotonic() - started_at
    child_pid = int((workspace / "child.pid").read_text(encoding="utf-8"))

    assert json.loads(result.content)["status"] == "timeout"
    assert elapsed < 1.5
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.asyncio
async def test_terminal_cancellation_terminates_shell_and_child(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    factory = ToolRuntimeFactory(workspace_root=workspace)
    contribution = load_builtin(
        "terminal",
        context=ToolLoadContext(workspace_root=workspace,
                                services={"runtime_factory": factory}),
    )
    task = asyncio.create_task(contribution.handler(
        ToolCallContext("run", "session", "agent"),
        {"command": "echo $$ > shell.pid; sleep 30 & echo $! > child.pid; wait", "timeout": 60},
    ))
    shell_pid = child_pid = None
    try:
        async with asyncio.timeout(3):
            while not (workspace / "child.pid").exists():
                await asyncio.sleep(0.01)
        shell_pid = int((workspace / "shell.pid").read_text())
        child_pid = int((workspace / "child.pid").read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        with pytest.raises(ProcessLookupError):
            os.kill(shell_pid, 0)
        # The grandchild is reaped by the OS subreaper, not this handler, so a
        # canceled run may observe it in any non-runnable state: "Z" while the
        # subreaper has not collected it yet, "X" while it is being torn down,
        # or already gone.  Only a runnable state means the child survived.
        try:
            state = next(line for line in Path(f"/proc/{child_pid}/status").read_text().splitlines()
                         if line.startswith("State:"))
        except (FileNotFoundError, ProcessLookupError):
            pass
        else:
            assert state.split()[1] in {"Z", "X"}, state
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if shell_pid is not None:
            try:
                os.killpg(shell_pid, 9)
            except ProcessLookupError:
                pass
        await asyncio.sleep(0.05)
        await factory.close()
