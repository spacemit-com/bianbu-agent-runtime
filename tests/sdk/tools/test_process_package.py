from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from bianbu_agent.tools.builtins import load_builtin, builtin_source

from bianbu_agent import ToolCallContext
from bianbu_agent.tools import (
    DirectoryToolLoader,
    ToolLoadContext,
    ToolRuntimeFactory,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_terminal_background_process_is_managed_without_the_registry(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"

    factory = ToolRuntimeFactory(workspace_root=workspace)
    loaded = {}
    for name in ("terminal", "process"):
        root = name
        loaded[name] = load_builtin(
            root,
            context=ToolLoadContext(
                workspace_root=workspace,
                services={"runtime_factory": factory},
            ),
        )
    call = ToolCallContext("run", "session", "agent")

    started = await loaded["terminal"].handler(
        call,
        {"command": "printf background-package", "background": True},
    )
    session_id = json.loads(started.content)["session_id"]
    waited = await loaded["process"].handler(
        call,
        {"action": "wait", "session_id": session_id, "timeout": 10},
    )

    assert json.loads(waited.content) == {
        "output": "background-package",
        "exit_code": 0,
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_process_package_supports_write_list_and_kill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    factory = ToolRuntimeFactory(workspace_root=workspace)
    loaded = {}
    for name in ("terminal", "process"):
        root = name
        loaded[name] = load_builtin(
            root,
            context=ToolLoadContext(
                workspace_root=workspace,
                services={"runtime_factory": factory},
            ),
        )
    call = ToolCallContext("run", "session", "agent")
    started = await loaded["terminal"].handler(
        call,
        {"command": "read line; sleep 30", "background": True},
    )
    session_id = json.loads(started.content)["session_id"]

    listed = await loaded["process"].handler(call, {"action": "list"})
    assert json.loads(listed.content)["processes"][0]["session_id"] == session_id
    written = await loaded["process"].handler(
        call,
        {"action": "write", "session_id": session_id, "data": "continue\n"},
    )
    assert json.loads(written.content)["status"] == "written"
    killed = await loaded["process"].handler(
        call,
        {"action": "kill", "session_id": session_id},
    )
    assert json.loads(killed.content)["status"] == "killed"


@pytest.mark.asyncio
async def test_process_package_supports_poll_and_paginated_log(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    factory = ToolRuntimeFactory(workspace_root=workspace)
    loaded = {}
    for name in ("terminal", "process"):
        root = name
        loaded[name] = load_builtin(
            root,
            context=ToolLoadContext(
                workspace_root=workspace,
                services={"runtime_factory": factory},
            ),
        )
    call = ToolCallContext("run", "session", "agent")
    started = await loaded["terminal"].handler(
        call,
        {"command": "printf 'one\\ntwo\\n'; sleep 1", "background": True},
    )
    session_id = json.loads(started.content)["session_id"]

    await asyncio.sleep(0.1)
    polled = await loaded["process"].handler(
        call, {"action": "poll", "session_id": session_id}
    )
    logged = await loaded["process"].handler(
        call, {"action": "log", "session_id": session_id, "offset": 1, "limit": 1}
    )

    assert json.loads(polled.content)["status"] == "running"
    assert json.loads(logged.content)["output"] == "two"
    await loaded["process"].handler(call, {"action": "kill", "session_id": session_id})


@pytest.mark.asyncio
async def test_process_records_are_private_to_the_starting_session(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    factory = ToolRuntimeFactory(workspace_root=workspace)
    loaded = {
        name: load_builtin(
            name,
            context=ToolLoadContext(
                workspace_root=workspace,
                services={"runtime_factory": factory},
            ),
        )
        for name in ("terminal", "process")
    }
    owner = ToolCallContext("run-owner", "session-owner", "agent")
    other = ToolCallContext("run-other", "session-other", "agent")
    started = await loaded["terminal"].handler(
        owner, {"command": "sleep 30", "background": True}
    )
    session_id = json.loads(started.content)["session_id"]

    try:
        listed = await loaded["process"].handler(other, {"action": "list"})
        polled = await loaded["process"].handler(
            other, {"action": "poll", "session_id": session_id}
        )
        assert json.loads(listed.content) == {"processes": []}
        assert json.loads(polled.content)["code"] == "process_not_found"
    finally:
        await loaded["process"].handler(
            owner, {"action": "kill", "session_id": session_id}
        )
        await factory.close()


@pytest.mark.asyncio
async def test_process_close_sends_eof_without_killing_the_command(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    factory = ToolRuntimeFactory(workspace_root=workspace)
    loaded = {
        name: load_builtin(
            name,
            context=ToolLoadContext(
                workspace_root=workspace,
                services={"runtime_factory": factory},
            ),
        )
        for name in ("terminal", "process")
    }
    call = ToolCallContext("run", "session", "agent")
    started = await loaded["terminal"].handler(
        call,
        {"command": "cat >/dev/null; printf eof-observed", "background": True},
    )
    session_id = json.loads(started.content)["session_id"]

    try:
        closed = await loaded["process"].handler(
            call, {"action": "close", "session_id": session_id}
        )
        waited = await loaded["process"].handler(
            call, {"action": "wait", "session_id": session_id, "timeout": 5}
        )
        assert json.loads(closed.content)["status"] == "closed"
        assert json.loads(waited.content) == {
            "output": "eof-observed",
            "exit_code": 0,
            "status": "completed",
        }
    finally:
        await factory.close()


@pytest.mark.asyncio
async def test_process_ignores_forged_workspace_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    factory = ToolRuntimeFactory(workspace_root=workspace)
    contribution = load_builtin(
        "process",
        context=ToolLoadContext(
            workspace_root=workspace,
            services={"runtime_factory": factory},
        ),
    )
    forged_root = workspace / ".bianbu-agent-processes"
    forged_root.mkdir(parents=True)
    session_id = "process-deadbeefcafe"
    (forged_root / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "owner_session_id": "session",
                "owner_agent_id": "agent",
                "pid": os.getpid(),
                "close_path": str(forged_root / f"{session_id}.close"),
                "output_path": str(forged_root / f"{session_id}.log"),
                "input_path": str(forged_root / f"{session_id}.stdin"),
            }
        ),
        encoding="utf-8",
    )
    called = False

    def killpg(_pid: int, _signal: int) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(os, "killpg", killpg)
    result = await contribution.handler(
        ToolCallContext("run", "session", "agent"),
        {"action": "kill", "session_id": session_id},
    )

    assert json.loads(result.content)["code"] == "process_not_found"
    assert called is False
