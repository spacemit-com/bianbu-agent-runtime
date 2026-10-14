from __future__ import annotations

import base64
import importlib.util
import json
import socket
from pathlib import Path

import pytest

from bianbu_agent.tools.builtins import load_builtin, builtin_source

from bianbu_agent import ToolCallContext
from bianbu_agent.tools import (
    DirectoryToolLoader,
    ToolLoadContext,
    ToolRuntimeFactory,
)
from bianbu_agent.tools.runtime_services import RuntimeServices

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _attach_url_module():
    path = builtin_source("kanban_attach_url")
    spec = importlib.util.spec_from_file_location("test_kanban_attach_url_tool", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tools(tmp_path: Path, names: tuple[str, ...]):
    workspace = tmp_path / "workspace"
    services = RuntimeServices(
        sessions_root=tmp_path / "sessions",
        workspace=workspace,
        model=None,
        runtime_home=tmp_path / "runtime",
    )

    factory = ToolRuntimeFactory(
        workspace_root=workspace,
        services=services,
    )
    loaded = {}
    for name in names:
        root = name
        loaded[name] = load_builtin(
            root,
            context=ToolLoadContext(
                workspace_root=workspace,
                services={"runtime_factory": factory},
            ),
        )
    return loaded


@pytest.mark.asyncio
async def test_kanban_lifecycle_is_owned_by_directory_packages(tmp_path: Path) -> None:
    tools = _tools(
        tmp_path,
        (
            "kanban_create",
            "kanban_list",
            "kanban_show",
            "kanban_block",
            "kanban_unblock",
            "kanban_complete",
            "kanban_comment",
            "kanban_link",
        ),
    )
    call = ToolCallContext("run", "session", "agent")
    parent = json.loads(
        (
            await tools["kanban_create"].handler(
                call, {"title": "Parent", "assignee": "researcher"}
            )
        ).content
    )["task"]
    child = json.loads(
        (
            await tools["kanban_create"].handler(
                call, {"title": "Child", "assignee": "writer"}
            )
        ).content
    )["task"]

    await tools["kanban_link"].handler(
        call, {"parent_id": parent["id"], "child_id": child["id"]}
    )
    await tools["kanban_comment"].handler(
        call, {"task_id": child["id"], "body": "Please verify."}
    )
    blocked = await tools["kanban_block"].handler(
        call, {"task_id": child["id"], "reason": "Waiting"}
    )
    await tools["kanban_unblock"].handler(call, {"task_id": child["id"]})
    completed = await tools["kanban_complete"].handler(
        call, {"task_id": parent["id"], "summary": "Done"}
    )
    shown = await tools["kanban_show"].handler(call, {"task_id": child["id"]})
    listed = await tools["kanban_list"].handler(call, {"assignee": "writer"})

    assert json.loads(blocked.content)["task"]["status"] == "blocked"
    assert json.loads(completed.content)["task"]["status"] == "done"
    shown_task = json.loads(shown.content)["task"]
    assert shown_task["parents"] == [parent["id"]]
    assert shown_task["comments"][0]["body"] == "Please verify."
    assert json.loads(listed.content)["tasks"][0]["id"] == child["id"]


@pytest.mark.asyncio
async def test_kanban_attachment_and_heartbeat_packages_share_board_state(
    tmp_path: Path,
) -> None:
    tools = _tools(
        tmp_path,
        (
            "kanban_create",
            "kanban_attach",
            "kanban_attachments",
            "kanban_heartbeat",
        ),
    )
    call = ToolCallContext("run", "session", "agent")
    task_id = json.loads(
        (
            await tools["kanban_create"].handler(
                call, {"title": "Deliver", "assignee": "worker"}
            )
        ).content
    )["task"]["id"]

    await tools["kanban_attach"].handler(
        call,
        {
            "task_id": task_id,
            "filename": "report.txt",
            "content_base64": base64.b64encode(b"report").decode(),
            "content_type": "text/plain",
        },
    )
    heartbeat = await tools["kanban_heartbeat"].handler(
        call, {"task_id": task_id, "note": "working"}
    )
    attachments = await tools["kanban_attachments"].handler(call, {"task_id": task_id})

    assert json.loads(heartbeat.content)["task"]["heartbeat"] == "working"
    assert json.loads(attachments.content)["attachments"][0]["filename"] == "report.txt"


@pytest.mark.asyncio
async def test_kanban_create_links_parents_and_completion_promotes_child(
    tmp_path: Path,
) -> None:
    tools = _tools(tmp_path, ("kanban_create", "kanban_complete", "kanban_show"))
    call = ToolCallContext("run", "session", "agent")
    parent = json.loads(
        (
            await tools["kanban_create"].handler(
                call, {"title": "Parent", "assignee": "one"}
            )
        ).content
    )["task"]
    child = json.loads(
        (
            await tools["kanban_create"].handler(
                call,
                {"title": "Child", "assignee": "two", "parents": [parent["id"]]},
            )
        ).content
    )["task"]

    assert child["status"] == "todo"
    shown_parent = json.loads(
        (await tools["kanban_show"].handler(call, {"task_id": parent["id"]})).content
    )["task"]
    assert shown_parent["children"] == [child["id"]]

    await tools["kanban_complete"].handler(call, {"task_id": parent["id"]})
    shown_child = json.loads(
        (await tools["kanban_show"].handler(call, {"task_id": child["id"]})).content
    )["task"]
    assert shown_child["status"] == "ready"


@pytest.mark.asyncio
async def test_kanban_create_rejects_missing_parent_without_mutation(
    tmp_path: Path,
) -> None:
    tools = _tools(tmp_path, ("kanban_create", "kanban_list"))
    call = ToolCallContext("run", "session", "agent")

    result = json.loads(
        (
            await tools["kanban_create"].handler(
                call,
                {"title": "Orphan", "assignee": "one", "parents": ["missing"]},
            )
        ).content
    )
    listed = json.loads((await tools["kanban_list"].handler(call, {})).content)

    assert result["code"] == "invalid_parent"
    assert listed["tasks"] == []


@pytest.mark.asyncio
async def test_kanban_boards_are_isolated(tmp_path: Path) -> None:
    tools = _tools(tmp_path, ("kanban_create", "kanban_show"))
    call = ToolCallContext("run", "session", "agent")
    task = json.loads(
        (
            await tools["kanban_create"].handler(
                call, {"title": "Alpha", "assignee": "one", "board": "alpha"}
            )
        ).content
    )["task"]

    result = json.loads(
        (
            await tools["kanban_show"].handler(
                call, {"task_id": task["id"], "board": "beta"}
            )
        ).content
    )

    assert result["code"] == "not_found"


@pytest.mark.asyncio
async def test_kanban_link_rejects_dependency_cycles(tmp_path: Path) -> None:
    tools = _tools(tmp_path, ("kanban_create", "kanban_link"))
    call = ToolCallContext("run", "session", "agent")
    first = json.loads(
        (
            await tools["kanban_create"].handler(
                call, {"title": "First", "assignee": "one"}
            )
        ).content
    )["task"]
    second = json.loads(
        (
            await tools["kanban_create"].handler(
                call, {"title": "Second", "assignee": "two"}
            )
        ).content
    )["task"]
    await tools["kanban_link"].handler(
        call, {"parent_id": first["id"], "child_id": second["id"]}
    )

    result = json.loads(
        (
            await tools["kanban_link"].handler(
                call, {"parent_id": second["id"], "child_id": first["id"]}
            )
        ).content
    )

    assert result["code"] == "dependency_cycle"


@pytest.mark.asyncio
async def test_kanban_corrupt_state_is_reported_without_overwrite(
    tmp_path: Path,
) -> None:
    tools = _tools(tmp_path, ("kanban_create", "kanban_show"))
    call = ToolCallContext("run", "session", "agent")
    task = json.loads(
        (
            await tools["kanban_create"].handler(
                call, {"title": "Task", "assignee": "one"}
            )
        ).content
    )["task"]
    state_files = list(
        (tmp_path / "workspace/.bianbu-agent-state/kanban").glob("*.json")
    )
    assert len(state_files) == 1
    state_files[0].write_text("{broken", encoding="utf-8")

    result = json.loads(
        (await tools["kanban_show"].handler(call, {"task_id": task["id"]})).content
    )

    assert result["code"] == "invalid_state"
    assert state_files[0].read_text(encoding="utf-8") == "{broken"


def test_kanban_attach_url_rejects_every_non_global_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _attach_url_module()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.16.0.2", 80))
        ],
    )
    with pytest.raises(ValueError, match="public"):
        module._download("http://private.example/file")


def test_kanban_attach_url_validates_redirect_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _attach_url_module()
    answers = iter(("93.184.216.34", "127.0.0.1"))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), 80))
        ],
    )
    requests = []

    def request_once(parsed, addresses):
        requests.append((parsed, addresses))
        return 302, "http://private.example/secret", b"", "text/plain"

    monkeypatch.setattr(module, "_request_once", request_once)
    with pytest.raises(ValueError, match="public"):
        module._download("http://public.example/file")
    assert len(requests) == 1


def test_kanban_attach_url_pins_the_validated_dns_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _attach_url_module()
    resolutions = 0

    def getaddrinfo(*_args, **_kwargs):
        nonlocal resolutions
        resolutions += 1
        address = "93.184.216.34" if resolutions == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

    def request_once(_parsed, addresses):
        assert addresses[0][4][0] == "93.184.216.34"
        return 200, None, b"safe", "text/plain"

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(module, "_request_once", request_once)

    assert module._download("http://rebind.example/file") == (b"safe", "text/plain")
    assert resolutions == 1
