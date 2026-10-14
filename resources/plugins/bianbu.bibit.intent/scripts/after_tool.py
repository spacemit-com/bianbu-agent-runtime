"""写文件后的依赖提醒 Hook（只读，绝不安装）。

模型用 `write_file` / `patch` 改完代码后，这里问一次 Bibit Intent 服务的
`dep_notice`（只读扫描）：还有依赖没装时，把那一行提醒作为
`context_sections` 附到工具结果里（Runtime 会拼成 "Hook context:"），
模型因此能顺口提醒用户，而不是等用户自己去撞 ModuleNotFoundError。

边界（对应 docs/design/dependency_management.md 的 M3-1）：
- 只调用只读工具，不安装、不弹授权、不改任何文件；
- 同一项目在 `_WINDOW_SECONDS` 内只扫一次，连续写文件不会反复扫描；
- 任何异常都吞掉并回空补丁：提示失败绝不能影响工具调用本身。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx

_WINDOW_SECONDS = 60.0
# executor 执行工具前要求「剩余预算 ≥ 40s」，比这更小会被判成
# edge_deadline_exceeded 而根本不派发；这里只借这个预算做服务端判定，
# 客户端等多久由 BIBIT_INTENT_NOTICE_TIMEOUT 决定（提示是尽力而为）。
_EDGE_TIMEOUT_MS = 60_000
_NOTICE_PREFIX = "依赖提醒"
_NOTICE_MISSING = "【未安装】"
_STATE_KEY = "dep_notice"
_GUIDANCE = (
    "这是只读提醒，本轮没有安装任何东西。如果确实改了代码，"
    "用一句话告诉用户缺什么即可；不要自行安装，也不要重复罗列依赖清单。"
)
_CODE_SUFFIXES = frozenset({
    ".py", ".pyi", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".rs", ".go", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml",
    ".cmake", ".mk", ".sh", ".js", ".ts",
})
_CODE_NAMES = frozenset({
    "makefile", "gnumakefile", "cmakelists.txt", "setup.py", "setup.cfg",
    "pyproject.toml", "requirements.txt", "package.json",
})
_REQUIREMENTS_RE = re.compile(r"requirements[-_].+\.txt$")
_V4A_FILE_RE = re.compile(r"^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)$", re.MULTILINE)


def _target_paths(tool_input: dict) -> list[str]:
    """本次写操作涉及的文件：`path` 参数，或 V4A 补丁正文里的文件头。"""
    paths: list[str] = []
    raw = str(tool_input.get("path") or "").strip()
    if raw:
        paths.append(raw)
    for match in _V4A_FILE_RE.finditer(str(tool_input.get("patch") or "")):
        candidate = match.group(1).strip()
        if candidate:
            paths.append(candidate)
    return paths


def _looks_like_code(path: str) -> bool:
    name = Path(path).name.casefold()
    return (
        Path(path).suffix.casefold() in _CODE_SUFFIXES
        or name in _CODE_NAMES
        or bool(_REQUIREMENTS_RE.fullmatch(name))
    )


def _resolves_inside(workspace: Path, path: str) -> bool:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        return candidate.resolve().is_relative_to(workspace)
    except (OSError, ValueError):
        return False


def _should_check(workspace: Path, tool_input: dict) -> bool:
    paths = _target_paths(tool_input)
    if not paths:
        # patch 模式可能只给补丁正文：拿不到文件名时宁可扫一次（有去抖）
        return True
    if not all(_resolves_inside(workspace, path) for path in paths):
        return False
    return any(_looks_like_code(path) for path in paths)


async def _notice(root: Path) -> str:
    base_url = os.environ.get("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
    timeout = float(os.environ.get("BIBIT_INTENT_NOTICE_TIMEOUT", "20"))
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(
            f"{base_url}/v1/runs",
            json={
                "run_id": uuid.uuid4().hex,
                "session_id": "bibit-dep-notice",
                "prompt": f"{_NOTICE_PREFIX} {root}",
                "timeout_ms": _EDGE_TIMEOUT_MS,
                "stream": False,
            },
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("handled"):
        return ""
    # 规则路径的 reply 会在提示行后附带工具的 detail JSON，只取提示那一行
    for line in str(payload.get("reply") or "").splitlines():
        line = line.strip()
        if line.startswith(_NOTICE_PREFIX):
            return line
    return ""


async def after_tool(run: dict) -> dict | None:
    if os.environ.get("BIBIT_INTENT_DEP_NOTICE", "1") == "0":
        return None
    raw_workspace = os.environ.get("PLUGIN_WORKSPACE_ROOT", "").strip()
    if not raw_workspace:
        return None
    workspace = Path(raw_workspace)
    if not _should_check(workspace, dict(run.get("tool_input") or {})):
        return None

    state = dict(run.get("state") or {})
    memo = state.get(_STATE_KEY)
    memo = dict(memo) if isinstance(memo, dict) else {}
    now = time.time()
    if memo.get("root") == str(workspace) and now - float(memo.get("at") or 0) < _WINDOW_SECONDS:
        return None
    state[_STATE_KEY] = {"root": str(workspace), "at": now}

    try:
        reply = await _notice(workspace)
    except Exception:  # noqa: BLE001 - 提示失败不能影响工具调用
        return {"state": state}
    if _NOTICE_MISSING not in reply:
        return {"state": state}
    return {
        "context_sections": [f"{reply}\n{_GUIDANCE}"],
        "state": state,
        "metadata": {"dep_notice": {"root": str(workspace), "status": "missing"}},
    }


if __name__ == "__main__":
    from protocol import main

    main(after_tool)
