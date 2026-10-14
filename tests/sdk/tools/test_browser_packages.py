from __future__ import annotations

import json
from pathlib import Path

import pytest

from bianbu_agent.tools.builtins import load_builtin, builtin_source

from bianbu_agent import ToolCallContext
from bianbu_agent.tools import DirectoryToolLoader, ToolLoadContext

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, workspace: Path):
    root = name
    return load_builtin(
        root,
        context=ToolLoadContext(workspace_root=workspace),
    )


def _fake_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "browser.jsonl"
    executable = tmp_path / "agent-browser"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['BROWSER_TEST_LOG'], 'a') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "print(json.dumps({'success': True, 'data': {'args': args}}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("BIANBU_AGENT_BROWSER_COMMAND", str(executable))
    monkeypatch.setenv("BROWSER_TEST_LOG", str(log))
    return log


@pytest.mark.asyncio
async def test_browser_packages_own_cli_command_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _fake_browser(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    call = ToolCallContext("run", "session", "agent")
    cases = (
        (
            "browser_navigate",
            {"url": "https://example.com"},
            "open",
            ("https://example.com",),
        ),
        ("browser_snapshot", {"full": False}, "snapshot", ("-c",)),
        ("browser_click", {"ref": "e2"}, "click", ("@e2",)),
        ("browser_type", {"ref": "@e3", "text": "hello"}, "fill", ("@e3", "hello")),
        ("browser_scroll", {"direction": "down"}, "scroll", ("down", "500")),
        ("browser_back", {}, "back", ()),
        ("browser_press", {"key": "Enter"}, "press", ("Enter",)),
    )
    for name, arguments, command, cli_arguments in cases:
        result = await _load(name, workspace).handler(call, arguments)
        assert json.loads(result.content)["success"] is True
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    for actual, (_, _, command, cli_arguments) in zip(calls, cases, strict=True):
        assert actual == ["--session", "session", "--json", command, *cli_arguments]


@pytest.mark.asyncio
async def test_browser_console_and_images_are_package_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _fake_browser(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    call = ToolCallContext("run", "session", "agent")

    await _load("browser_console", workspace).handler(
        call, {"expression": "document.title"}
    )
    await _load("browser_console", workspace).handler(call, {"clear": True})
    await _load("browser_get_images", workspace).handler(call, {})

    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls[0][-2:] == ["eval", "document.title"]
    assert calls[1][-2:] == ["console", "--clear"]
    assert calls[2][-2] == "eval"
    assert "document.images" in calls[2][-1]
