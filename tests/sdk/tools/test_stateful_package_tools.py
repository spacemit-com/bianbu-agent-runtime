from __future__ import annotations

import json
from pathlib import Path

import pytest

from bianbu_agent import ToolCallContext
from bianbu_agent.tools.builtins import load_builtin

from bianbu_agent.tools import (
    ToolLoadContext,
    ToolRuntimeFactory,
)


def _load(name: str, tmp_path: Path):
    return load_builtin(name, context=ToolLoadContext(
        workspace_root=tmp_path / 'workspace', services={'runtime_factory': ToolRuntimeFactory(workspace_root=tmp_path / 'workspace')}))


@pytest.mark.asyncio
async def test_todo_package_publishes_complete_list_without_package_state(
    tmp_path: Path,
) -> None:
    tool = _load("todo", tmp_path)
    call = ToolCallContext("run", "session", "agent")
    result = await tool.handler(
        call,
        {
            "todos": [
                {"id": "1", "status": "completed", "content": "First"},
                {"id": "2", "content": "Second", "status": "pending"},
            ],
        },
    )

    assert json.loads(result.content) == {
        "todos": [
            {"id": "1", "content": "First", "status": "completed"},
            {"id": "2", "content": "Second", "status": "pending"},
        ],
        "summary": {
            "pending": 1,
            "in_progress": 0,
            "completed": 1,
            "cancelled": 0,
            "total": 2,
        },
    }
    assert set(tool.spec.input_schema["properties"]) == {"todos"}
    assert tool.spec.input_schema["required"] == ["todos"]
    data_root = tmp_path / "tools" / "todo" / "data"
    assert not data_root.exists() or not any(data_root.rglob("*"))
