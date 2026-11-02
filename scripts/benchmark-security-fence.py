#!/usr/bin/env python3
"""Benchmark the Runtime-side security fence for N tool calls.

Measures three distinct costs so they are not conflated:

1. in-process guard logic (middleware wrapping + DLP reference lookup),
2. one bwrap tool sandbox process per call,
3. amortised agent-mode fence overhead (one bwrap startup for N calls).

This does not include securityd D-Bus policy decisions; run it on a board
with bianbu-security active to measure that boundary as well.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(samples_ms: list[float]) -> dict[str, float]:
    return {
        "count": len(samples_ms),
        "total_ms": round(sum(samples_ms), 3),
        "mean_ms": round(statistics.fmean(samples_ms), 3),
        "median_ms": round(statistics.median(samples_ms), 3),
        "p95_ms": round(_percentile(samples_ms, 95), 3),
        "p99_ms": round(_percentile(samples_ms, 99), 3),
        "min_ms": round(min(samples_ms), 3),
        "max_ms": round(max(samples_ms), 3),
        "stdev_ms": round(statistics.pstdev(samples_ms), 3),
    }


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _request(*, tool: str = "echo", args: dict[str, Any] | None = None):
    control = SimpleNamespace(session_id="bench-session", request_id="bench-run")
    runtime = SimpleNamespace(context=SimpleNamespace(control=control))
    return SimpleNamespace(
        tool_call={
            "name": tool,
            "args": args if args is not None else {"text": "hello"},
            "id": "bench-call",
        },
        runtime=runtime,
        tool_metadata={},
    )


async def _echo(request):
    return SimpleNamespace(content="ok")


def _run_coroutine(fn: Callable[[], Any]) -> Any:
    return asyncio.run(fn())


def bench_plain(n: int) -> dict[str, Any]:
    async def one() -> None:
        await _echo(_request())

    samples: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        _run_coroutine(one)
        samples.append((time.perf_counter() - started) * 1000)
    return _summary(samples)


def bench_middleware(n: int, *, enabled: bool) -> dict[str, Any]:
    from bianbu_agent.security.security_middleware import BianbuSecurityMiddleware

    middleware = BianbuSecurityMiddleware(enabled=enabled)
    request = _request()

    async def one() -> None:
        await middleware.awrap_tool_call(request, _echo)

    # Warm imports so the first-call langgraph import is reported separately,
    # not hidden inside the 1000-call distribution.
    warm_started = time.perf_counter()
    _run_coroutine(one)
    warm_ms = (time.perf_counter() - warm_started) * 1000

    samples: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        _run_coroutine(one)
        samples.append((time.perf_counter() - started) * 1000)
    result = _summary(samples)
    result["first_call_ms"] = round(warm_ms, 3)
    return result


def _sandbox_argv(*, python: str, source: str, net: bool) -> list[str]:
    """Equivalent of SandboxConfig(mode=tool) for this host's merged-usr layout.

    The repository's _merged_usr_binds() skips /bin and /lib when they already
    exist, which leaves the relative symlinks dangling inside newer bwrap.
    Recreate them explicitly so this benchmark measures fence startup rather
    than a path-resolution failure.
    """
    argv = [
        "bwrap",
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--cap-drop", "ALL", "--die-with-parent",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/etc", "/etc",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    if net and Path("/sys").exists():
        argv += ["--ro-bind", "/sys", "/sys"]
    if not net:
        argv.append("--unshare-net")
    argv += ["--", python, "-c", source]
    return argv


def bench_sandbox_process(n: int, *, python: str, net: bool) -> dict[str, Any]:
    argv = _sandbox_argv(python=python, source="pass", net=net)
    samples: list[float] = []
    errors = 0
    for _ in range(n):
        started = time.perf_counter()
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        samples.append((time.perf_counter() - started) * 1000)
        if proc.returncode != 0:
            errors += 1
    result = _summary(samples)
    result["errors"] = errors
    result["argv_head"] = argv[:8]
    return result


def bench_sandbox_payload(n: int, *, python: str, net: bool) -> dict[str, Any]:
    """Same subprocess, but executes a JSON tool invocation in the fence."""
    source = (
        "import json,sys\n"
        "spec=json.loads(sys.stdin.read())\n"
        "args=spec['args']\n"
        "sys.stdout.write(json.dumps({'content': args.get('text',''), "
        "'metadata': {}}))\n"
    )
    argv = _sandbox_argv(python=python, source=source, net=net)
    payload = json.dumps({"args": {"text": "hello"}})
    samples: list[float] = []
    errors = 0
    for _ in range(n):
        started = time.perf_counter()
        proc = subprocess.run(
            argv, input=payload, capture_output=True, text=True, timeout=120
        )
        samples.append((time.perf_counter() - started) * 1000)
        if proc.returncode != 0:
            errors += 1
    result = _summary(samples)
    result["errors"] = errors
    return result


def bench_agent_fence(n: int, *, python: str, net: bool) -> dict[str, Any]:
    """One bwrap startup, N tool calls inside it (mode=agent semantics)."""
    source = (
        "import json,sys,time\n"
        "spec=json.loads(sys.stdin.read())\n"
        "out=[]\n"
        "for _ in range(spec['n']):\n"
        "    started=time.perf_counter()\n"
        "    value={'text':'hello'}\n"
        "    out.append((time.perf_counter()-started)*1000)\n"
        "sys.stdout.write(json.dumps(out))\n"
    )
    argv = _sandbox_argv(python=python, source=source, net=net)
    started = time.perf_counter()
    proc = subprocess.run(
        argv,
        input=json.dumps({"n": n}),
        capture_output=True,
        text=True,
        timeout=120,
    )
    wall_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-500:])
    inner = json.loads(proc.stdout)
    result = _summary([float(x) for x in inner])
    result["inner_total_ms"] = round(sum(float(x) for x in inner), 6)
    result["wall_ms"] = round(wall_ms, 3)
    result["startup_ms"] = round(wall_ms - result["total_ms"], 3)
    result["startup_per_call_ms"] = round(
        max(0.0, wall_ms - result["total_ms"]) / n, 4
    )
    return result


def bench_middleware_mocked_security(n: int) -> dict[str, Any]:
    """Middleware with an in-process ALLOW decision (no securityd round-trip)."""
    from bianbu_agent.security import security_middleware as middleware

    original_mode = middleware.agent_security_mode_is_yolo
    original_decision = middleware.securityd_approval_decision
    middleware.agent_security_mode_is_yolo = lambda *_a, **_k: False
    middleware.securityd_approval_decision = lambda *_a, **_k: middleware.APPROVAL_ALLOW
    try:
        middleware_instance = middleware.BianbuSecurityMiddleware(enabled=True)
        request = _request()

        async def one() -> None:
            await middleware_instance.awrap_tool_call(request, _echo)

        _run_coroutine(one)
        samples: list[float] = []
        for _ in range(n):
            started = time.perf_counter()
            _run_coroutine(one)
            samples.append((time.perf_counter() - started) * 1000)
        return _summary(samples)
    finally:
        middleware.agent_security_mode_is_yolo = original_mode
        middleware.securityd_approval_decision = original_decision


def _markdown(report: dict[str, Any]) -> str:
    env = report["environment"]
    lines = [
        "# 安全围栏性能基准报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 测试主机：{env['hostname']} / {env['machine']} / {env['platform']}",
        f"- Python 版本：{env['python']}",
        f"- 每场景调用次数：{report['calls']}",
        f"- securityd 状态：{'运行中' if env['securityd_active'] else '未运行'}"
        f"{'（' + env.get('securityd_unit', '') + '）' if env.get('securityd_unit') else ''}",
        f"- bwrap 版本：{env['bwrap_version'] or '未安装'}",
        "",
        "安全围栏有两类独立开销：每次调用都要执行的进程内判权逻辑，以及调用跨过",
        " bwrap 边界时的进程/命名空间启动成本。`agent` 模式下每个 Worker 只启动",
        "一次 bwrap；`tool` 模式下每次工具调用都会新建一个 bwrap 进程。",
        "",
        "## 测试结果",
        "",
        "| 场景 | 总耗时 ms | 平均 ms | 中位数 ms | P95 ms | P99 ms | 错误数 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    labels = {
        "plain_call": "无围栏裸调用",
        "middleware_allow": "安全中间件：进程内 ALLOW",
        "middleware_failclosed": "安全中间件：securityd 离线回退",
        "sandbox_process": "tool 模式：每次调用启动 bwrap",
        "sandbox_payload": "tool 模式：bwrap + JSON 工具参数往返",
        "agent_fence_amortized": "agent 模式：bwrap 启动一次，N 次调用在围栏内",
    }
    for key in (
        "plain_call",
        "middleware_allow",
        "middleware_failclosed",
        "sandbox_process",
        "sandbox_payload",
        "agent_fence_amortized",
    ):
        item = report["results"].get(key)
        if not item:
            continue
        lines.append(
            "| {label} | {total_ms} | {mean_ms} | {median_ms} | {p95_ms} | "
            "{p99_ms} | {errors} |".format(
                label=labels.get(key, key),
                total_ms=item.get("total_ms", "-"),
                mean_ms=item.get("mean_ms", "-"),
                median_ms=item.get("median_ms", "-"),
                p95_ms=item.get("p95_ms", "-"),
                p99_ms=item.get("p99_ms", "-"),
                errors=item.get("errors", 0),
            )
        )
    amortized = report["results"].get("agent_fence_amortized")
    if amortized:
        lines += [
            "",
            f"agent 模式下，{report['calls']} 次调用的总墙钟时间为 "
            f"{amortized['wall_ms']:.3f} ms；bwrap 启动成本摊销到每次调用为 "
            f"{amortized['startup_per_call_ms']:.4f} ms。",
        ]
    lines += ["", "## 开销归因", ""]
    plain = report["results"]["plain_call"]
    allow = report["results"]["middleware_allow"]
    sandbox = report["results"]["sandbox_process"]
    guard_total = allow["total_ms"] - plain["total_ms"]
    fence_total = sandbox["total_ms"] - plain["total_ms"]
    lines += [
        f"- 单次进程内判权逻辑（中位数差值）："
        f"**{allow['median_ms'] - plain['median_ms']:.3f} ms**",
        f"- {report['calls']} 次判权总计（总耗时差值）："
        f"**{guard_total:.3f} ms**",
        f"- tool 模式单次 bwrap 边界成本（中位数）："
        f"**{sandbox['median_ms']:.3f} ms**",
        f"- {report['calls']} 次 tool 模式围栏总计（总耗时差值）："
        f"**{fence_total:.1f} ms**",
        "",
        "## 结论",
        "",
        f"- 如果围栏随 Worker 启动一次（`agent` 模式），{report['calls']} 次调用"
        f"的额外开销约为 **{guard_total:.1f} ms**，单次约 "
        f"**{guard_total / report['calls']:.4f} ms**。",
        f"- 如果每次工具调用都单独启动围栏（`tool` 模式），{report['calls']} 次"
        f"调用的额外开销约为 **{fence_total / 1000:.1f} 秒**，单次约 "
        f"**{sandbox['median_ms']:.1f} ms**。",
        *(
            ["- securityd 在本机运行；本报告仍未包含其 D-Bus 策略判权延迟，",
             "  该边界需要由已注册 Agent 的 Worker 身份调用才能测量。"]
            if env["securityd_active"] else
            ["- 正常 securityd 常驻时，还需要叠加 D-Bus 判权延迟；本机 securityd",
             "  未运行，离线回退数据不代表正常判权成本。"]
        ),
        "",
        "## 测量限制",
        "",
        "- `middleware_failclosed` 包含本机 securityd 桥接回退，这是环境失败路径，",
        "  不是正常的 ALLOW 成本。",
        "- 沙箱场景使用 `/usr/bin/python3` 并显式补充 merged-usr 符号链接，原因是",
        "  本机 bwrap 不接受仓库默认参数；围栏的系统调用与命名空间限制本身未改变。",
        *(
            ["- securityd 在本机运行，但基准进程不是已注册 Agent 的 Worker，",
             "  因此未包含其 D-Bus 策略判权延迟。"]
            if env["securityd_active"] else
            ["- 本机 securityd 未运行，因此未包含 securityd D-Bus 策略判权延迟。"]
        ),
        "- 本次测试直接调用安全模块与 bwrap，没有启动专家 Agent，也没有调用任何",
        "  LLM；结果反映 Runtime 侧围栏开销，不包含模型推理时间。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--calls", type=int, default=1000)
    parser.add_argument("--python", default="/usr/bin/python3")
    parser.add_argument("--net", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--skip-sandbox", action="store_true")
    args = parser.parse_args()

    securityd_active = False
    securityd_unit = ""
    for unit in ("bianbu-securityd", "bianbu-security"):
        try:
            if subprocess.run(
                ["systemctl", "is-active", "--quiet", unit], timeout=3
            ).returncode == 0:
                securityd_active = True
                securityd_unit = unit
                break
        except Exception:
            continue
    if not securityd_active:
        try:
            securityd_active = subprocess.run(
                ["pgrep", "-x", "securityd"], capture_output=True, timeout=3
            ).returncode == 0
        except Exception:
            pass
    bwrap_version = ""
    if shutil.which("bwrap"):
        try:
            bwrap_version = subprocess.run(
                ["bwrap", "--version"], capture_output=True, text=True, timeout=3
            ).stdout.strip()
        except Exception:
            pass

    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "calls": args.calls,
        "environment": {
            "hostname": platform.node(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "securityd_active": securityd_active,
            "securityd_unit": securityd_unit,
            "bwrap_version": bwrap_version,
            "rss_start_mb": round(_rss_mb(), 1),
        },
        "results": {},
    }

    report["results"]["plain_call"] = bench_plain(args.calls)
    report["results"]["middleware_allow"] = bench_middleware_mocked_security(
        args.calls
    )
    report["results"]["middleware_failclosed"] = bench_middleware(
        args.calls, enabled=True
    )
    if not args.skip_sandbox:
        report["results"]["sandbox_process"] = bench_sandbox_process(
            args.calls, python=args.python, net=args.net
        )
        report["results"]["sandbox_payload"] = bench_sandbox_payload(
            args.calls, python=args.python, net=args.net
        )
        report["results"]["agent_fence_amortized"] = bench_agent_fence(
            args.calls, python=args.python, net=args.net
        )
    report["environment"]["rss_end_mb"] = round(_rss_mb(), 1)

    output = Path(args.output) if args.output else (
        ROOT / "docs" / "benchmarks" / "security-fence-1000.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_markdown(report), encoding="utf-8")
    json_path = output.with_suffix(".json")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(_markdown(report))
    print(f"\nSaved: {output}\nSaved: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
