"""`expert-creator` 技能的契约。

这个技能经 Runtime 的真实加载器必须可加载，并且它教模型做的每一步都要在今天的
Runtime 上真的存在：

- SKILL.md 里写出来的 `bianbu-agent <资源> <动作>` 必须是真实 CLI 的子命令。
  2026-09-08 的 `5fe86036` 把 `init/check/build/install/run/stop/logs` 从 CLI 删了
  （CLI 改为只管理资源、不启动运行），技能必须跟着改，不能再教模型调用它们；
- 专家身份由 Runtime 生成：脚本只走 `/api/v1/agents`（POST 新建、PATCH 更新），
  不直接写 `agents/<ID>/agent.yaml`，也不走开发用的 `.agent` 项目 API；
- 同名专家默认拒绝创建，替换必须由用户点名 `--update`。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from bianbu_agent.cli.parser import build_parser
from bianbu_agent.skills.loader import DirectorySkillLoader

SKILL_ROOT = Path(__file__).resolve().parents[3] / "resources" / "skills" / "expert-creator"
SCRIPT = SKILL_ROOT / "scripts" / "create_expert.py"
CREATED_ID = "com.bianbu.0123456789ab"
EXISTING_ID = "com.bianbu.existing0001"
REMOVED_LIFECYCLE = ("init", "check", "build", "install", "run", "stop", "logs")

_FENCED_BLOCK = re.compile(r"^[ \t]*```([a-zA-Z0-9_-]*)[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE)


def _instructions() -> str:
    return DirectorySkillLoader().load(SKILL_ROOT).spec.instructions


def _code_blocks(text: str, languages: set[str]) -> list[str]:
    """取出标注了语言的围栏代码块；``languages`` 为空集合时取全部。"""

    return [
        body
        for language, body in _FENCED_BLOCK.findall(text)
        if not languages or language.lower() in languages
    ]


def _documented_commands(text: str) -> set[tuple[str, str]]:
    """SKILL.md 的 shell 代码块里出现的 `bianbu-agent ... <资源> <动作>` 组合。

    只认可执行代码块：正文或 `text` 块里提到旧命令不算"教模型去调用"。
    一层命令（如 `bianbu-agent serve`）的动作记为空串。
    """

    commands = set()
    for block in _code_blocks(text, {"bash", "sh", "shell"}):
        for match in re.finditer(
            r"bianbu-agent(?:\s+--config\s+\S+)?\s+([a-z][a-z-]*)"
            r"(?:\s+([a-z][a-z-]*))?",
            block,
        ):
            commands.add((match.group(1), match.group(2) or ""))
    return commands


def _cli_commands() -> set[tuple[str, str]]:
    """真实 CLI 支持的 (资源, 动作) 组合，含 `agent show` 这类三层命令。"""

    commands: set[tuple[str, str]] = set()

    def walk(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        nested = False
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            nested = True
            for name, subparser in action.choices.items():
                walk(subparser, (*path, name))
        if len(path) >= 2:
            commands.add((path[0], path[1]))
        elif path and not nested:
            commands.add((path[0], ""))

    walk(build_parser(), ())
    return commands


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("expert_creator_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRuntime:
    """最小 Runtime：只实现脚本会用到的 `/api/v1/agents`。

    `PATCH` 按真实契约做局部更新：只覆盖请求体里出现的字段，`metadata`
    这类对象整体替换，因此"没点名的字段保持不变"是真的在测。
    """

    def __init__(self, *, agents: list[dict] | None = None, status: int = 201) -> None:
        self.requests: list[dict] = []
        self.agents = {agent["id"]: dict(agent) for agent in agents or []}
        self._status = status
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, method: str) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                entry = {"method": method, "path": self.path,
                         "body": json.loads(raw) if raw else None}
                runtime.requests.append(entry)
                return entry

            def do_GET(self) -> None:  # noqa: N802
                self._record("GET")
                if self.path.startswith("/api/v1/agents/"):
                    agent_id = self.path.rsplit("/", 1)[-1]
                    agent = runtime.agents.get(agent_id)
                    if agent is None:
                        self._reply(404, {"detail": "not found"})
                        return
                    self._reply(200, {"data": agent})
                    return
                self._reply(200, {"data": list(runtime.agents.values())})

            def do_POST(self) -> None:  # noqa: N802
                entry = self._record("POST")
                agent = {
                    "id": CREATED_ID,
                    **entry["body"],
                    "available": True,
                    "unavailable_reason": "",
                    "missing_powers": [],
                }
                runtime.agents[CREATED_ID] = dict(agent)
                self._reply(runtime._status, {"data": agent})

            def do_PATCH(self) -> None:  # noqa: N802
                entry = self._record("PATCH")
                agent_id = entry["path"].rsplit("/", 1)[-1]
                agent = dict(runtime.agents.get(agent_id) or {"id": agent_id})
                agent.update(entry["body"])
                agent["available"] = True
                agent.setdefault("unavailable_reason", "")
                agent.setdefault("missing_powers", [])
                runtime.agents[agent_id] = agent
                self._reply(200, {"data": agent})

            def log_message(self, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def runtime() -> Any:
    server = _FakeRuntime()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def installed_runtime() -> Any:
    """已经装了一个带头像、能力和 execution 元数据的专家。"""

    server = _FakeRuntime(agents=[{
        "id": EXISTING_ID, "name": "Writer", "name_zh": "写作专家",
        "description": "写作", "soul": "写。",
        "model": {"provider": "custom:bibit", "name": "deepseek-flash"},
        "tools": ["shell"], "skills": ["using-apm"], "mcps": [], "plugins": [],
        "metadata": {"avatar": "译", "execution": {"requested_mode": "host"}},
        "available": True, "unavailable_reason": "", "missing_powers": [],
    }])
    try:
        yield server
    finally:
        server.close()


def test_skill_loads_with_the_real_loader() -> None:
    metadata, files = DirectorySkillLoader.inspect(SKILL_ROOT)

    assert metadata["name"] == SKILL_ROOT.name
    assert metadata["description"].strip()
    assert "\n" not in metadata["description"]
    assert [path.relative_to(SKILL_ROOT).as_posix() for path in files] == [
        "SKILL.md", "references/agent-schema.md", "scripts/create_expert.py",
    ]
    assert _instructions().strip()


def test_documented_cli_commands_exist_in_the_real_cli() -> None:
    documented = _documented_commands(_instructions())

    assert documented, "SKILL.md 必须给出可执行的 CLI 命令"
    assert documented <= _cli_commands()


def test_documented_lifecycle_is_marked_removed() -> None:
    instructions = _instructions()

    # 可执行代码块里绝不能再出现被删掉的旧生命周期。
    executable = "\n".join(_code_blocks(instructions, {"bash", "sh", "shell"}))
    for legacy in REMOVED_LIFECYCLE:
        assert f"bianbu-agent {legacy}" not in executable

    # 它们只能出现在明确指出"已移除"的说明块里。
    marked_removed = [block for block in _code_blocks(instructions, set()) if "已移除" in block]
    assert len(marked_removed) == 1
    assert all(legacy in marked_removed[0] for legacy in REMOVED_LIFECYCLE)
    prose = instructions
    for block in marked_removed:
        prose = prose.replace(block, "")
    for legacy in REMOVED_LIFECYCLE:
        assert f"bianbu-agent {legacy}" not in prose

    assert "5fe86036" in instructions
    assert "agent show" in instructions and "agent verify" in instructions


def test_script_creates_the_expert_through_the_runtime_api(
    runtime: _FakeRuntime, capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()

    exit_code = script.main([
        "--base-url", runtime.base_url,
        "--name", "TranslationExpert", "--name-zh", "翻译专家",
        "--description", "中英双向翻译专家",
        "--soul", "忠实翻译，不增删原意。",
        "--provider", "custom:bibit", "--model", "deepseek-flash",
        "--avatar", "译",
    ])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "expert_id": CREATED_ID, "name": "TranslationExpert",
        "available": True, "unavailable_reason": "", "missing_powers": [],
    }
    posted = [entry for entry in runtime.requests if entry["method"] == "POST"]
    assert len(posted) == 1
    assert posted[0]["path"] == "/api/v1/agents"
    assert posted[0]["body"] == {
        "name": "TranslationExpert", "name_zh": "翻译专家",
        "name_en": "", "description": "中英双向翻译专家",
        "description_zh": "", "soul": "忠实翻译，不增删原意。",
        "model": {"provider": "custom:bibit", "name": "deepseek-flash"},
        "tools": [], "skills": [], "mcps": [], "plugins": [],
        "metadata": {"avatar": "译"},
    }


def test_script_posts_the_english_and_chinese_display_fields(
    runtime: _FakeRuntime, capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()

    exit_code = script.main([
        "--base-url", runtime.base_url,
        "--name", "TranslationExpert", "--name-zh", "翻译专家",
        "--name-en", "Translation Expert",
        "--description", "Bidirectional Chinese/English translation expert.",
        "--description-zh", "中英双向翻译专家", "--soul", "x",
        "--provider", "custom:bibit", "--model", "deepseek-flash",
    ])

    assert exit_code == 0
    posted = [entry for entry in runtime.requests if entry["method"] == "POST"]
    assert posted[0]["body"]["name_en"] == "Translation Expert"
    assert posted[0]["body"]["description_zh"] == "中英双向翻译专家"
    assert posted[0]["body"]["name_zh"] == "翻译专家"

    exit_code = script.main([
        "--base-url", runtime.base_url, "--update", CREATED_ID,
        "--name-en", "Translation Expert v2", "--description-zh", "新版简介",
    ])

    assert exit_code == 0
    patched = [entry for entry in runtime.requests if entry["method"] == "PATCH"]
    assert patched[0]["body"] == {
        "name_en": "Translation Expert v2", "description_zh": "新版简介",
    }


def test_selectors_reject_a_wildcard_mixed_with_names(
    runtime: _FakeRuntime, capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()

    exit_code = script.main([
        "--base-url", runtime.base_url,
        "--name", "Any", "--soul", "x",
        "--provider", "custom:bibit", "--model", "deepseek-flash",
        "--skill", "*", "--skill", "spacemit-power/vibe-coder",
    ])

    assert exit_code == 1
    assert "wildcard must be the only entry" in capsys.readouterr().err
    assert runtime.requests == []


def test_script_updates_an_existing_expert_without_creating_a_second(
    installed_runtime: _FakeRuntime, capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()

    exit_code = script.main([
        "--base-url", installed_runtime.base_url, "--update", EXISTING_ID,
        "--name", "TranslationExpert", "--soul", "改过的 soul。",
        "--provider", "custom:bibit", "--model", "deepseek-flash",
    ])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["expert_id"] == EXISTING_ID
    assert [entry["method"] for entry in installed_runtime.requests] == ["GET", "PATCH"]
    assert installed_runtime.requests[1]["path"] == f"/api/v1/agents/{EXISTING_ID}"
    assert installed_runtime.agents[EXISTING_ID]["name"] == "TranslationExpert"
    assert installed_runtime.agents[EXISTING_ID]["name_zh"] == "写作专家"


def test_update_touches_only_the_fields_the_user_named(
    installed_runtime: _FakeRuntime, capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()

    exit_code = script.main([
        "--base-url", installed_runtime.base_url, "--update", EXISTING_ID,
        "--soul", "改过的 soul。",
    ])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["expert_id"] == EXISTING_ID
    assert [entry["method"] for entry in installed_runtime.requests] == ["PATCH"]
    assert installed_runtime.requests[0]["body"] == {"soul": "改过的 soul。"}
    # 没点名的字段必须保持原样：头像、能力、执行模式都不能被默认值冲掉。
    updated = installed_runtime.agents[EXISTING_ID]
    assert updated["metadata"] == {
        "avatar": "译", "execution": {"requested_mode": "host"},
    }
    assert updated["tools"] == ["shell"]
    assert updated["skills"] == ["using-apm"]
    assert updated["name_zh"] == "写作专家"


def test_update_with_an_avatar_merges_the_existing_metadata(
    installed_runtime: _FakeRuntime, capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()

    exit_code = script.main([
        "--base-url", installed_runtime.base_url, "--update", EXISTING_ID,
        "--avatar", "新",
    ])

    assert exit_code == 0
    capsys.readouterr()
    # metadata 是被整体替换的，所以脚本要先读回现值再合并。
    assert [(entry["method"], entry["path"]) for entry in installed_runtime.requests] == [
        ("GET", f"/api/v1/agents/{EXISTING_ID}"),
        ("PATCH", f"/api/v1/agents/{EXISTING_ID}"),
    ]
    assert installed_runtime.requests[1]["body"] == {
        "metadata": {"avatar": "新", "execution": {"requested_mode": "host"}},
    }
    assert installed_runtime.agents[EXISTING_ID]["soul"] == "写。"


def test_update_needs_at_least_one_field(
    installed_runtime: _FakeRuntime, capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()

    exit_code = script.main([
        "--base-url", installed_runtime.base_url, "--update", EXISTING_ID,
    ])

    assert exit_code == 1
    assert "至少要给出一个" in capsys.readouterr().err
    assert installed_runtime.requests == []


def test_update_refuses_to_rename_onto_another_expert(
    installed_runtime: _FakeRuntime, capsys: pytest.CaptureFixture[str],
) -> None:
    installed_runtime.agents["com.bianbu.taken00000001"] = {
        "id": "com.bianbu.taken00000001", "name": "Taken", "name_zh": "",
    }
    script = _load_script()

    exit_code = script.main([
        "--base-url", installed_runtime.base_url, "--update", EXISTING_ID,
        "--name", "Taken",
    ])

    assert exit_code == 1
    assert "com.bianbu.taken00000001" in capsys.readouterr().err
    assert [entry["method"] for entry in installed_runtime.requests] == ["GET"]


def test_creation_defaults_the_avatar_and_requires_name_soul_and_model(
    runtime: _FakeRuntime, capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()
    common = ["--base-url", runtime.base_url]

    assert script.main([*common, "--name", "NoModel", "--soul", "x"]) == 1
    assert "--provider 和 --model" in capsys.readouterr().err
    assert script.main([*common, "--name", "NoSoul", "--provider", "custom:bibit"]) == 1
    assert "--soul" in capsys.readouterr().err
    assert script.main([*common, "--soul", "x", "--provider", "custom:bibit",
                        "--model", "deepseek-flash"]) == 1
    assert "--name" in capsys.readouterr().err
    assert runtime.requests == []

    assert script.main([*common, "--name", "Plain", "--soul", "x",
                        "--provider", "custom:bibit", "--model", "deepseek-flash"]) == 0
    capsys.readouterr()
    assert runtime.requests[-1]["body"]["metadata"] == {"avatar": "🤖"}


def test_script_refuses_a_duplicate_name_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    server = _FakeRuntime(agents=[{"id": "com.bianbu.existing0001", "name": "Writer",
                                   "name_zh": "写作专家"}])
    try:
        script = _load_script()
        exit_code = script.main([
            "--base-url", server.base_url,
            "--name", "Writer", "--soul", "写。",
            "--provider", "custom:bibit", "--model", "deepseek-flash",
        ])
    finally:
        server.close()

    assert exit_code == 1
    assert "com.bianbu.existing0001" in capsys.readouterr().err
    assert [entry["method"] for entry in server.requests] == ["GET"]


def test_script_reports_an_unreachable_runtime(capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script()

    exit_code = script.main([
        "--base-url", "http://127.0.0.1:1",
        "--name", "Writer", "--soul", "写。",
        "--provider", "custom:bibit", "--model", "deepseek-flash",
    ])

    assert exit_code == 1
    assert "连不上 Runtime" in capsys.readouterr().err


def test_script_does_not_write_the_data_directory_itself() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for forbidden in ("init_agent_project", "check_agent_project", "build_agent_project",
                      "ManifestAgentStore", "Path.home()"):
        assert forbidden not in source
    assert "/api/v1/agents" in source
