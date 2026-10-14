#!/usr/bin/env python3
"""把一个 Agent 的 Worker 灌到 scope 的内存限额，复现 memcg OOM 归因。

背景与取证见历史缺陷记录（Worker cgroup OOM 归因）。两条路径：

- 路径 B（默认，不动任何配置）：同一个 Worker 不重启，连续跑长回答。Worker 的
  常驻内存来自 ``MemoryRunStore``（保存该 Worker 生成周期内所有 Run 的流式事件，
  ``finish()`` 不回收），因此会单调上涨，直到撞上 scope 的 ``MemoryMax``。
- 路径 A（``--memory-max-mib``，分钟级）：把 scope 的 ``MemoryMax`` 压到当前用量
  附近再跑一轮，立刻触顶；适合不想等自然累积的时候。

脚本从不重启 Worker：scope 名里的 hash 来自 ``execution.instance_id``，hash 变了
就说明 Worker 已被 OOM 回收并重新拉起。

示例：

    # 只看当前读数（等价于 watch，Ctrl-C 退出）
    ./scripts/reproduce-worker-cgroup-oom.py --agent-id com.bianbu.278200e1617c --watch

    # 自然累积：跑 5 轮长回答，每轮打印读数与增量
    ./scripts/reproduce-worker-cgroup-oom.py --agent-id com.bianbu.278200e1617c --rounds 5

    # 快路径：压到 800 MiB 再跑一轮（需要 root；脚本会先打印命令）
    sudo ./scripts/reproduce-worker-cgroup-oom.py --agent-id com.bianbu.278200e1617c \
        --memory-max-mib 800 --apply-limit
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

MIB = 1024 * 1024
BAR_WIDTH = 40
DEFAULT_API = "http://127.0.0.1:9899/api/v1"
DEFAULT_AGENT_ID = "com.bianbu.278200e1617c"
CGROUP_ROOT = Path(os.environ.get("BIANBU_CGROUP_ROOT", "/sys/fs/cgroup"))
PROC_ROOT = Path(os.environ.get("BIANBU_PROC_ROOT", "/proc"))

DEFAULT_PROMPT = (
    "请用中文写一篇不少于 3000 字的详细技术说明，主题：RISC-V 架构的发展历史、"
    "主要指令集扩展与生态现状。要求分章节展开，尽量详尽，不要调用任何工具，直接输出正文。"
)


@dataclass(frozen=True)
class Sample:
    """一个 Worker scope 的内存读数（单位：字节）。"""

    current: int
    peak: int
    limit: int | None
    oom_kill: int
    processes: int


def parse_limit(value: str) -> int | None:
    """``memory.max`` 里的 ``max`` 表示无上限，返回 None。"""

    text = value.strip()
    if text == "max":
        return None
    return int(text)


def read_sample(scope: Path) -> Sample | None:
    """读取 scope 读数；scope 已被回收时返回 None。"""

    try:
        current = int((scope / "memory.current").read_text().strip())
        peak = int((scope / "memory.peak").read_text().strip())
        limit = parse_limit((scope / "memory.max").read_text())
    except (OSError, ValueError):
        return None
    oom_kill = 0
    try:
        for line in (scope / "memory.events").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                oom_kill = int(value.strip())
    except (OSError, ValueError):
        oom_kill = 0
    return Sample(current, peak, limit, oom_kill, count_processes(scope))


def count_processes(scope: Path) -> int:
    try:
        return len([line for line in (scope / "cgroup.procs").read_text().split() if line])
    except OSError:
        return 0


def scope_processes(scope: Path, limit: int = 3) -> list[tuple[int, str, int]]:
    """按 RSS 取 scope 内占用最大的几个进程：(pid, 名字, RSS 字节)。"""

    try:
        pids = [int(line) for line in (scope / "cgroup.procs").read_text().split() if line]
    except OSError:
        return ()
    rows: list[tuple[int, str, int]] = []
    for pid in pids:
        try:
            status = (PROC_ROOT / str(pid) / "status").read_text()
        except OSError:
            continue
        name = ""
        rss = 0
        for line in status.splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
                break
        rows.append((pid, name, rss))
    rows.sort(key=lambda row: row[2], reverse=True)
    return rows[:limit]


def format_mib(value: int) -> str:
    return f"{value / MIB:.0f} MiB"


def format_percent(current: int, limit: int | None) -> float:
    if not limit:
        return 0.0
    return current / limit * 100


def format_reading(sample: Sample) -> str:
    """给用户看的读数，形状与板上 watch 输出一致。"""

    if sample.limit:
        used = format_percent(sample.current, sample.limit)
        head = (
            f"Worker 内存  {format_mib(sample.current)} / {format_mib(sample.limit)}"
            f"  ({used:.0f}%)"
        )
    else:
        used = 0.0
        head = f"Worker 内存  {format_mib(sample.current)} / 无上限"
    filled = int(BAR_WIDTH * min(used, 100.0) / 100 + 0.5)
    bar = "#" * filled + "." * (BAR_WIDTH - filled)
    return "\n".join([head, f"历史峰值     {format_mib(sample.peak)}", f"[{bar}]"])


def scope_from_pid(pid: int) -> Path | None:
    try:
        text = (PROC_ROOT / str(pid) / "cgroup").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3 or fields[0] != "0":
            continue
        relative = fields[2].strip().lstrip("/")
        if relative:
            return CGROUP_ROOT / relative
    return None


def scope_from_hash(instance_suffix: str) -> Path | None:
    """按 instance_id 后缀找 scope（Worker 重启后 pid 会变，hash 仍唯一）。"""

    slice_dir = CGROUP_ROOT / "system.slice"
    if not slice_dir.is_dir():
        return None
    matches = sorted(slice_dir.glob(f"bianbu-agent-*{instance_suffix}.scope"))
    return matches[0] if matches else None


def resolve_scope(execution: dict[str, Any]) -> Path | None:
    instance_id = str(execution.get("instance_id") or "")
    pid = execution.get("pid")
    if isinstance(pid, int) and pid > 0:
        scope = scope_from_pid(pid)
        if scope is not None:
            return scope
    if len(instance_id) >= 12:
        return scope_from_hash(instance_id[-12:])
    return None


def request_json(
    base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def agent_execution(base: str, agent_id: str) -> dict[str, Any]:
    body = request_json(base, "GET", f"/agents/{agent_id}")
    data = body.get("data") or {}
    execution = data.get("execution") or {}
    if not execution:
        raise RuntimeError(f"agent {agent_id} has no execution state yet: {data}")
    return execution


def create_session(base: str, agent_id: str, title: str) -> str:
    body = request_json(base, "POST", "/sessions", {"agent_id": agent_id, "title": title})
    return str((body.get("data") or {})["id"])


def session_events(base: str, session_id: str, timeout: float = 30.0) -> dict[str, Any]:
    return request_json(base, "GET", f"/sessions/{session_id}/events?max_messages=50", None, timeout)


def send_prompt(base: str, session_id: str, message_id: str, content: str) -> str | None:
    body = request_json(
        base,
        "POST",
        f"/sessions/{session_id}/prompt",
        {"message_id": message_id, "content": content},
    )
    return (body.get("data") or {}).get("run_id")


def run_terminal_event(body: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    """在会话事件里找该 Run 的终态事件（run/completed、run/failed、run/cancelled）。"""

    for item in reversed(list(body.get("data") or [])):
        if not isinstance(item, dict) or item.get("run_id") != run_id:
            continue
        if str(item.get("type")) in {"run/completed", "run/failed", "run/cancelled"}:
            return item
    return None


def usage_totals(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {str(key): int(value) for key, value in usage.items() if isinstance(value, (int, float))}


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def print_reading(sample: Sample, scope: Path, prefix: str = "") -> None:
    head = f"{prefix}" if prefix else ""
    if head:
        print(head, flush=True)
    print(format_reading(sample), flush=True)
    if sample.limit:
        remaining = max(sample.limit - sample.current, 0)
        print(f"距限额还剩   {format_mib(remaining)}", flush=True)
    top = scope_processes(scope)
    if top:
        detail = " / ".join(f"{name or pid} {format_mib(rss)}" for pid, name, rss in top)
        print(f"最大占用     {detail}", flush=True)


def parse_memory_max(mib: int) -> str:
    return f"MemoryMax={mib}M"


def systemctl_set_property(unit: str, property_text: str, *, apply: bool) -> None:
    command = ["systemctl", "set-property", unit, property_text]
    if os.geteuid() != 0:
        command = ["sudo", *command]
    if not apply:
        log(f"未加 --apply-limit，只打印命令：{' '.join(command)}")
        return
    log(f"执行 {' '.join(command)}")
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"systemctl 失败：{result.stderr.strip() or result.stdout.strip()}")
    log(f"已把 {unit} 的内存限额改成 {property_text}")


def estimate_rounds(sample: Sample, delta: int, stop_ratio: float) -> int | None:
    """按上一轮增量估算还要多少轮才到 ``stop_ratio`` 的限额。"""

    if not sample.limit or delta <= 0:
        return None
    budget = sample.limit * stop_ratio - sample.current
    if budget <= 0:
        return 0
    return int(budget // delta)


def run_round(
    base: str,
    session_id: str,
    prompt: str,
    run_timeout: float,
    poll_interval: float,
    scope: Path,
    poll_hook: Callable[[Sample | None], None] | None = None,
) -> dict[str, Any]:
    """发一轮提示词，边等边打印读数，返回该轮的终态信息。"""

    message_id = f"oom-repro-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    before = session_events(base, session_id)
    base_seq = int(before.get("last_seq") or 0)
    run_id = send_prompt(base, session_id, message_id, prompt)
    if not run_id:
        raise RuntimeError("prompt 被接收但没有 run_id；换一个空闲会话再试")
    log(f"已起一轮 run={run_id}（message_id={message_id}）")
    deadline = started + run_timeout
    scope_gone = False
    while True:
        sample = read_sample(scope)
        if poll_hook is not None:
            poll_hook(sample)
        if sample is None and not scope_gone:
            scope_gone = True
            log("scope 已消失，继续等这一轮的终态事件来区分 OOM 与 Worker 重启")
        body = session_events(base, session_id)
        terminal = run_terminal_event(body, run_id)
        if terminal is not None:
            payload = dict(terminal.get("data") or {})
            seq_delta = int(body.get("last_seq") or 0) - base_seq
            return {
                "run_id": run_id,
                "status": str(terminal.get("type")),
                "scope_gone": scope_gone,
                "payload": payload,
                "usage": usage_totals(payload),
                "elapsed": time.monotonic() - started,
                "events": seq_delta,
            }
        if time.monotonic() > deadline:
            raise TimeoutError(f"run {run_id} 超过 {run_timeout:.0f}s 仍未结束")
        time.sleep(poll_interval)


def refreshed_scope(base: str, agent_id: str) -> Path | None:
    """每轮开始前重新解析 scope：Worker 重启会换 hash，旧路径会失效。"""

    try:
        return resolve_scope(agent_execution(base, agent_id))
    except (urllib.error.URLError, RuntimeError, KeyError):
        return None


def run_watch(scope: Path, interval: float, rounds: int) -> int:
    """只盯读数，不跑轮次（等价于 watch -n <interval>）。"""

    log(f"盯 {scope}（Ctrl-C 退出）")
    seen = 0
    while True:
        sample = read_sample(scope)
        if sample is None:
            log("scope 已消失（Worker 被回收或已重启，scope hash 会变）")
            return 1
        print_reading(sample, scope)
        seen += 1
        if rounds and seen >= rounds:
            return 0
        time.sleep(interval)


def watch_scope(sample: Sample | None) -> None:
    if sample is None:
        log("本轮中 scope 被回收：Worker 已不在，继续等终态事件")
        return
    print(format_reading(sample), flush=True)


def drive(args: argparse.Namespace) -> int:
    execution = agent_execution(args.api, args.agent_id)
    scope = resolve_scope(execution)
    if scope is None:
        raise RuntimeError(f"没找到 {args.agent_id} 的 Worker scope：{execution}")
    log(f"agent={args.agent_id} state={execution.get('state')} pid={execution.get('pid')}")
    log(f"scope={scope}")

    sample = read_sample(scope)
    if sample is None:
        raise RuntimeError(f"scope 读不到读数：{scope}")
    if args.watch:
        return run_watch(scope, args.interval, args.rounds)
    print_reading(sample, scope, prefix="起始读数")

    unit = scope.name
    original_limit = sample.limit
    applied = False
    if args.memory_max_mib:
        command_text = parse_memory_max(args.memory_max_mib)
        systemctl_set_property(unit, command_text, apply=args.apply_limit)
        applied = args.apply_limit
        if applied:
            log(f"等 {args.settle:.0f}s 让 supervisor 心跳（5s 一次）采样新限额")
            time.sleep(args.settle)
            updated = read_sample(scope)
            if updated is not None:
                print_reading(updated, scope, prefix="压限额后读数")

    session_id = args.session_id
    if session_id:
        log(f"复用会话 {session_id}")
    else:
        title = f"Worker OOM 复现 {time.strftime('%Y-%m-%d %H:%M:%S')}"
        session_id = create_session(args.api, args.agent_id, title)
        log(f"新会话 {session_id}（title={title}）")

    rounds: list[dict[str, Any]] = []
    round_index = 0
    try:
        while True:
            round_index += 1
            if args.rounds and round_index > args.rounds:
                break
            current_scope = refreshed_scope(args.api, args.agent_id)
            if current_scope is not None and current_scope != scope:
                log(f"Worker 已重启：scope 从 {scope.name} 变成 {current_scope.name}，内存清零重新计数")
                scope = current_scope
                unit = scope.name
            prompt = args.prompt
            if round_index > 1:
                prompt = f"{prompt}\n（这是第 {round_index} 轮；请再写一篇完整的长文，不要复述上一轮。）"
            start_sample = read_sample(scope)
            print(f"\n=== 第 {round_index} 轮 ===", flush=True)
            if start_sample is None:
                log("scope 已消失，无法继续")
                return 1
            print_reading(start_sample, scope, prefix="本轮起始")
            result = run_round(
                args.api, session_id, prompt, args.run_timeout, args.interval, scope,
                poll_hook=watch_scope,
            )
            end_sample = read_sample(scope)
            delta = 0 if end_sample is None else end_sample.current - start_sample.current
            log(
                f"第 {round_index} 轮结束：{result['status']}，用时 {result['elapsed']:.1f}s，"
                f"会话事件 +{result.get('events', 0)} 条"
            )
            payload = result.get("payload") or {}
            error = payload.get("error")
            if isinstance(error, dict) and error:
                log(f"错误：code={error.get('code')} message={error.get('message')}")
                details = error.get("details")
                if details:
                    log(f"      details={json.dumps(details, ensure_ascii=False)}")
            usage = result.get("usage") or {}
            if usage:
                log(
                    "token："
                    + ", ".join(f"{key}={value}" for key, value in sorted(usage.items()))
                )
            if end_sample is not None:
                print_reading(end_sample, scope, prefix="本轮结束")
                log(f"本轮内存增量 {delta / MIB:+.0f} MiB")
                stop_ratio = args.stop_at / 100 if args.stop_at else 1.0
                left = estimate_rounds(end_sample, delta, stop_ratio)
                if left is not None:
                    log(f"按本轮增量估算：距 {stop_ratio * 100:.0f}% 限额还需约 {left} 轮")
            rounds.append({
                "round": round_index,
                "status": result["status"],
                "run_id": result.get("run_id"),
                "elapsed_seconds": round(result["elapsed"], 1),
                "events": result.get("events"),
                "usage": usage,
                "error": error if isinstance(error, dict) else None,
                "memory_before": None if start_sample is None else start_sample.current,
                "memory_after": None if end_sample is None else end_sample.current,
                "memory_peak": None if end_sample is None else end_sample.peak,
                "scope_gone": bool(result.get("scope_gone")),
                "delta_bytes": delta,
            })
            if isinstance(error, dict) and error.get("code") == "worker_oom_killed":
                print("\n== 复现成功 ==", flush=True)
                log("这一轮 Worker 被 memcg OOM 杀死，Run 以 worker_oom_killed 失败")
                log("取证：journalctl -k -b | grep -i oom-kill")
                log(f"取证：curl -s {args.api}/sessions/{session_id}/events | tail -c 900")
                return 0
            if result.get("scope_gone"):
                log("scope 在这一轮里消失，但 Run 没有 worker_oom_killed 归因：多半是 Worker 被重启（内存已清零）")
                return 3
            if result["status"] != "run/completed":
                if not args.keep_going:
                    log("该轮不是正常完成，也不是 OOM 归因；停下来等人工判断（要跳过这类瞬时失败加 --keep-going）")
                    return 2
                log("该轮不是正常完成，也不是 OOM 归因；--keep-going 跳过，继续下一轮")
            if end_sample is not None and args.stop_at and end_sample.limit:
                used = format_percent(end_sample.current, end_sample.limit)
                if used >= args.stop_at:
                    log(
                        f"内存已到 {used:.0f}%，达到 --stop-at {args.stop_at:.0f}%，"
                        "先停手（要真触发 OOM，加 --until-oom 或调大 --stop-at）"
                    )
                    return 0
    finally:
        if args.report:
            Path(args.report).write_text(
                json.dumps(
                    {
                        "agent_id": args.agent_id,
                        "session_id": session_id,
                        "scope": str(scope),
                        "unit": unit,
                        "memory_max_mib": args.memory_max_mib,
                        "rounds": rounds,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            log(f"报告已写入 {args.report}")
        if applied and original_limit:
            if scope.exists():
                systemctl_set_property(unit, f"MemoryMax={original_limit}", apply=True)
            else:
                log("scope 已被回收，限额覆盖随 unit 一起消失，无需还原")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="复现 Worker 触顶 cgroup MemoryMax 被 OOM 杀死（历史缺陷：Worker cgroup OOM 归因）",
    )
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID, help="目标 Agent（默认 Bibit 端侧专家）")
    parser.add_argument("--api", default=DEFAULT_API, help=f"Runtime API 基地址（默认 {DEFAULT_API}）")
    parser.add_argument("--session-id", help="复用已有会话；默认新建一个")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="每轮发送的提示词")
    parser.add_argument("--rounds", type=int, default=0, help="跑几轮；0 表示一直跑到触发 OOM 或停手")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询/盯读数间隔秒数（默认 5s）")
    parser.add_argument("--run-timeout", type=float, default=900.0, help="单轮最长等待秒数")
    parser.add_argument("--watch", action="store_true", help="只盯读数，不发消息")
    parser.add_argument("--memory-max-mib", type=int, help="路径 A：把 scope 限额压到该 MiB 数")
    parser.add_argument("--apply-limit", action="store_true", help="真正执行 set-property（否则只打印命令）")
    parser.add_argument("--settle", type=float, default=12.0, help="压限额后等待心跳采样的秒数")
    parser.add_argument("--stop-at", type=float, default=95.0, help="用量达到该百分比就停手（默认 95）")
    parser.add_argument("--until-oom", action="store_true", help="不设停手阈值，跑到 OOM 为止")
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="非 OOM 的失败（provider ReadError 等）也跳过，继续下一轮累积内存",
    )
    parser.add_argument("--report", type=Path, help="把每轮读数写成 JSON 报告")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.until_oom:
        args.stop_at = 0.0
    if shutil.which("systemctl") is None and args.memory_max_mib:
        parser.error("--memory-max-mib 需要 systemctl")
    try:
        return drive(args)
    except KeyboardInterrupt:
        log("已手动中断")
        return 130
    except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
        log(f"失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
