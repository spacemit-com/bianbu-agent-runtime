"""单工具沙箱执行（INT-RT-17，mode=tool）。

把单个工具的执行放进 bwrap 子进程。与 mode=agent / mode=mcp 的区别是
粒度：Agent 可指定"只有 python_repl 和 shell 进沙箱，其余照常"。

## 适用边界（重要）

内置工具是**进程内 Python 函数**，出进程要靠序列化往返。以下工具**不适用**：

  · 持有连接/句柄的（DB session、HTTP client、打开的文件描述符）
  · 依赖进程内状态的（读写模块级变量、缓存、单例）
  · 返回不可序列化对象的（自定义类实例、生成器、闭包）

因此本实现采取**显式白名单 + 启动期校验**：只有在 `metadata.sandbox.tools`
里列出的工具才走沙箱，且工具须能通过 JSON 往返。校验失败时**明确报错**，
不静默回落到进程内执行 —— 用户以为隔离生效却没生效，比报错危险。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from bianbu_agent.security.security_sandbox import (
    SandboxConfig,
    SandboxConfigError,
    build_bwrap_argv,
)

logger = logging.getLogger("bianbu_agent.security.tool_runner")

__all__ = [
    "run_tool_sandboxed",
    "ToolSandboxError",
    "ensure_serializable",
]

# 子进程侧的执行器：读 stdin 的 JSON 规格，import 工具并调用，结果写 stdout。
# 用 -c 内联传入，避免依赖沙箱内的文件路径。
_RUNNER_SRC = r"""
import importlib, json, sys, asyncio

spec = json.loads(sys.stdin.read())
mod = importlib.import_module(spec["module"])
target = mod
for part in spec["attr"].split("."):
    target = getattr(target, part)

args = spec["args"]
ctx = spec.get("context") or {}

result = target(ctx, args) if spec.get("pass_context") else target(**args)
if asyncio.iscoroutine(result):
    result = asyncio.run(result)

# 只回传可 JSON 序列化的部分；工具返回对象时取其 content/metadata
if hasattr(result, "content"):
    payload = {"content": result.content,
               "metadata": dict(getattr(result, "metadata", {}) or {})}
else:
    payload = {"content": result, "metadata": {}}

# 返回值也必须真的可 JSON 往返 —— 不能用 default=str 兜。
# 用 default=str 的话，工具返回富对象时会被静默转成 "<object object at 0x...>"
# 这类垃圾串，agent 拿它当真数据用；而同一个工具不进沙箱时返回的是可用对象。
# 这种"配了沙箱反而语义变了"正是 SandboxConfigError 的设计要避免的。
# 故改为显式失败，由父进程翻译成可操作的提示。
try:
    encoded = json.dumps(payload)
except (TypeError, ValueError) as exc:
    sys.stderr.write("BIANBU_UNSERIALIZABLE_RESULT: %s" % exc)
    raise SystemExit(3)
sys.stdout.write("\x00BIANBU_RESULT\x00" + encoded)
"""

_SENTINEL = "\x00BIANBU_RESULT\x00"


class ToolSandboxError(RuntimeError):
    """沙箱内工具执行失败。"""


def ensure_serializable(args: Mapping[str, Any], tool_name: str) -> dict:
    """校验参数可 JSON 往返。不可序列化则明确报错，不静默降级。"""
    try:
        return json.loads(json.dumps(dict(args)))
    except (TypeError, ValueError) as e:
        raise ToolSandboxError(
            f"工具 {tool_name} 配置了 mode=tool 沙箱，但参数无法 JSON 序列化"
            f"（{e}）。该工具不适用单工具沙箱 —— 请从 "
            f"metadata.sandbox.tools 中移除，或改用 mode=agent。") from e


def run_tool_sandboxed(
    *,
    tool_name: str,
    module: str,
    attr: str,
    args: Mapping[str, Any],
    cfg: SandboxConfig,
    context: Mapping[str, Any] | None = None,
    pass_context: bool = True,
    timeout: float = 120.0,
) -> dict:
    """在 bwrap 子进程内执行工具，返回 {"content":..., "metadata":...}。

    module/attr 指向工具的可导入位置（如 "mypkg.tools" / "read_file"）。
    """
    safe_args = ensure_serializable(args, tool_name)

    spec = {
        "module": module,
        "attr": attr,
        "args": safe_args,
        "context": dict(context or {}),
        "pass_context": pass_context,
    }

    # 沙箱内要能 import 到工具模块 → 把当前 sys.path 的真实目录挂进去（只读）
    # Runtime is normally launched from an isolated virtualenv while the
    # Agent Memory SDK is installed by the ``bianbu-agent-memory`` Debian
    # package in the system dist-packages directory.  Tool plugins execute in
    # their own bubblewrap child, so inheriting the Worker interpreter's
    # ``sys.path`` alone is not sufficient to import the SDK.  Add this
    # immutable, package-managed location explicitly; it is already covered
    # by the namespace's read-only ``/usr`` mount.
    system_site = "/usr/lib/python3/dist-packages"
    extra_ro = tuple(
        dict.fromkeys(
            p for p in (*sys.path, system_site)
            if p and Path(p).is_dir()
        )
    )
    try:
        cmd, argv = build_bwrap_argv(
            sys.executable, ["-c", _RUNNER_SRC], cfg, extra_ro=extra_ro)
    except SandboxConfigError as e:
        raise ToolSandboxError(
            f"工具 {tool_name} 配置了沙箱但无法启动：{e}") from e

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(extra_ro)

    logger.info("工具 %s 在 bwrap 沙箱内执行（net=%s）", tool_name, cfg.net)
    try:
        proc = subprocess.run(
            [cmd, *argv], input=json.dumps(spec), capture_output=True,
            text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise ToolSandboxError(
            f"工具 {tool_name} 在沙箱内执行超时（{timeout}s）") from e

    if proc.returncode != 0:
        # rc=3 是 runner 约定的"返回值不可序列化"，翻成与参数侧一致的可操作提示
        if proc.returncode == 3 and "BIANBU_UNSERIALIZABLE_RESULT" in (proc.stderr or ""):
            why = (proc.stderr or "").split("BIANBU_UNSERIALIZABLE_RESULT:", 1)[-1].strip()
            raise ToolSandboxError(
                f"工具 {tool_name} 配置了 mode=tool 沙箱，但**返回值**无法 JSON "
                f"序列化（{why[:200]}）。该工具不适用单工具沙箱 —— 请从 "
                f"metadata.sandbox.tools 中移除，或改用 mode=agent。")
        raise ToolSandboxError(
            f"工具 {tool_name} 在沙箱内执行失败（rc={proc.returncode}）："
            f"{(proc.stderr or '')[-400:]}")

    idx = proc.stdout.rfind(_SENTINEL)
    if idx < 0:
        raise ToolSandboxError(
            f"工具 {tool_name} 未回传结果（stdout 无标记）。"
            f"该工具可能不适用单工具沙箱：{(proc.stdout or '')[-200:]}")
    try:
        return json.loads(proc.stdout[idx + len(_SENTINEL):])
    except json.JSONDecodeError as e:
        raise ToolSandboxError(
            f"工具 {tool_name} 回传结果非合法 JSON：{e}") from e
