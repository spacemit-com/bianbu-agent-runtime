#!/usr/bin/env python3
"""黑盒量「点击发送 → 首字」：POST prompt 记 t0，再按会话日志的事件时间戳拆账。

为什么不用 Runtime 自己的耗时统计：用户能感知的起点是点击发送（HTTP 请求发出），
终点是第一个 token 事件。两者都在会话日志里有独立时间戳，所以这里从 HTTP 侧发起
一轮真实 Run，然后读 ``session.jsonl.zstd`` 算：

- ``click_ms``：首字事件时间 − t0，即用户感知的首字延迟；
- ``api_ms``：``POST /sessions/{id}/prompt`` 自身的往返耗时；
- ``gap_ms``：``user/message`` → 最早 ``hook/invoked``，即「提交 → 第一个 Hook」；
- ``ttft_s``：``user/message`` → 最早 delta / tool 事件；
- ``hooks``：每个 ``hook/result`` 的 (plugin, point, 耗时)，按耗时倒序取前几个。

``--turns`` 让同一个会话连发多轮：第 N+1 轮在上一轮首个 token 之后 ``--turn-delay``
秒发出（模拟「看到回答就开始打下一句」），用来量上一轮 AfterRun 观察 Hook 是否
挡住了下一轮。多轮时每轮按 run_id 拆事件，不会互相串。

用法见 docs/perf/ttft-budget.md；源码 checkout 的数据目录不在 ``~/.bianbu-agent`` 时
（``run.sh`` 会把 sessions 写在 checkout 的 ``.bianbu-agent`` 下）用
``--sessions-root`` 指过去。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
import uuid

TERMINAL_EVENTS = ("run/completed", "run/failed", "run/cancelled")
FIRST_TOKEN_EVENTS = (
    "assistant/text.delta",
    "assistant/reasoning.delta",
    "tool/call.started",
)
POLL_SECONDS = 0.2


def request(base: str, method: str, path: str, payload: object = None) -> object:
    data = None if payload is None else json.dumps(payload).encode()
    http_request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(http_request, timeout=120) as response:
        return json.load(response)


def read_events(path: str) -> list[dict]:
    try:
        raw = subprocess.run(["zstd", "-dc", "-q", path], capture_output=True).stdout
    except OSError:
        return []
    events: list[dict] = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "event":
            events.append(record["event"])
    return events


def poll_events(path: str, *, deadline: float, ready) -> tuple[list[dict], bool]:
    """轮询会话日志，直到 ``ready(events)`` 成立或超时。"""

    events: list[dict] = []
    while True:
        if os.path.exists(path):
            events = read_events(path)
            if ready(events):
                return events, True
        if time.time() >= deadline:
            return events, False
        time.sleep(POLL_SECONDS)


def newest_user_seq(path: str) -> int:
    """当前会话日志里最后一条 user/message 的 seq（用于区分下一轮）。"""

    if not os.path.exists(path):
        return 0
    users = [event for event in read_events(path) if event.get("type") == "user/message"]
    return max((int(event.get("seq") or 0) for event in users), default=0)


def turn_events(events: list[dict], after_seq: int) -> list[dict]:
    """只看 ``after_seq`` 之后那一条 user/message 所属的那一轮。"""

    users = [
        event for event in events
        if event.get("type") == "user/message" and int(event.get("seq") or 0) > after_seq
    ]
    if not users:
        return []
    run_id = max(users, key=lambda event: int(event.get("seq") or 0)).get("run_id")
    return [event for event in events if event.get("run_id") == run_id]


def turn_record(*, turn: int, session_id: str, started: float, api_ms: float,
                events: list[dict], args) -> dict:
    user = next((event for event in events if event.get("type") == "user/message"), None)
    first_hook = next((event for event in events if event.get("type") == "hook/invoked"), None)
    first_token = next(
        (event for event in events if event.get("type") in FIRST_TOKEN_EVENTS), None)
    hooks = []
    for event in events:
        if event.get("type") != "hook/result":
            continue
        data = event.get("data") or {}
        hooks.append({
            "plugin": data.get("plugin_id"),
            "point": data.get("point"),
            "hook": data.get("hook_id"),
            "status": data.get("status"),
            "ms": round(float(data.get("duration_ms") or 0), 1),
            "at_ms": None if user is None
            else round((event["time"] - user["time"]) * 1000, 1),
        })
    hooks.sort(key=lambda hook: -hook["ms"])
    return {
        "session": session_id,
        "turn": turn,
        "user_epoch": None if user is None else round(float(user["time"]), 3),
        "api_ms": round(api_ms, 1),
        "gap_ms": None if not (user and first_hook)
        else round((first_hook["time"] - user["time"]) * 1000, 1),
        "ttft_s": None if not (user and first_token)
        else round(first_token["time"] - user["time"], 3),
        "click_s": None if first_token is None
        else round(first_token["time"] - started, 3),
        "hooks": hooks[: args.hooks],
        "term": next((event["type"] for event in events
                      if event.get("type") in TERMINAL_EVENTS), None),
    }


def run_session(args, index: int) -> list[dict]:
    if args.session:
        session_id = args.session
    else:
        session = request(args.base, "POST", "/sessions",
                          {"agent_id": args.agent, "title": f"ttft-probe-{index}"})
        session_id = session["data"]["id"]
    path = os.path.join(args.sessions_root, session_id, "session.jsonl.zstd")
    records: list[dict] = []
    for turn in range(1, args.turns + 1):
        started = time.time()
        baseline_seq = newest_user_seq(path)
        request(args.base, "POST", f"/sessions/{session_id}/prompt", {
            "message_id": uuid.uuid4().hex,
            "content": args.prompt,
            "attachments": [],
            "skill_ids": [],
        })
        api_ms = (time.time() - started) * 1000
        first_seen: float | None = None
        deadline = time.time() + args.timeout
        while True:
            all_events, _ = poll_events(
                path,
                deadline=deadline,
                ready=lambda seen: bool(turn_events(seen, baseline_seq)) and (
                    any(event.get("type") in TERMINAL_EVENTS
                        for event in turn_events(seen, baseline_seq))
                    or any(event.get("type") in FIRST_TOKEN_EVENTS
                           for event in turn_events(seen, baseline_seq))
                ),
            )
            events = turn_events(all_events, baseline_seq)
            if first_seen is None and any(
                event.get("type") in FIRST_TOKEN_EVENTS for event in events
            ):
                first_seen = time.time()
            if first_seen is not None and any(
                event.get("type") in TERMINAL_EVENTS for event in events
            ):
                break
            if first_seen is None and time.time() >= deadline:
                break
        if turn < args.turns:
            # 模拟「看到首字就开始打下一句」：从首个 token 起再等 turn_delay。
            if first_seen is not None:
                remaining = args.turn_delay - (time.time() - first_seen)
                if remaining > 0:
                    time.sleep(remaining)
            elif args.turn_delay > 0:
                time.sleep(args.turn_delay)
            records.append(turn_record(turn=turn, session_id=session_id,
                                       started=started, api_ms=api_ms,
                                       events=events, args=args))
            continue
        events, _ = poll_events(
            path,
            deadline=time.time() + args.timeout,
            ready=lambda seen: any(
                event.get("type") in TERMINAL_EVENTS
                for event in turn_events(seen, baseline_seq)
            ),
        )
        records.append(turn_record(turn=turn, session_id=session_id,
                                   started=started, api_ms=api_ms,
                                   events=turn_events(events, baseline_seq), args=args))
    if not args.keep and not args.session:
        try:
            request(args.base, "DELETE", f"/sessions/{session_id}")
        except Exception as exc:  # noqa: BLE001 - 清理失败不该毁掉这一轮测量
            print(f"cleanup failed: {exc}", file=sys.stderr)
    return records


def summarise(records: list[dict], key: str, label: str) -> str:
    values = sorted(record[key] for record in records if record[key] is not None)
    if not values:
        return f"[{label}] {key}: 无样本"
    return (f"[{label}] {key} p50={statistics.median(values):.2f} "
            f"min={values[0]:.2f} max={values[-1]:.2f} n={len(values)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="?", type=int, default=3, help="测量轮数（会话数）")
    parser.add_argument("--turns", type=int, default=1, help="每个会话连发几轮")
    parser.add_argument("--turn-delay", type=float, default=2.0,
                        help="下一轮在上一轮首个 token 之后多少秒发出")
    parser.add_argument("--base", default=os.environ.get(
        "BIANBU_AGENT_BASE_URL", "http://127.0.0.1:9899/api/v1"))
    parser.add_argument("--agent", default=os.environ.get("COM_BIANBU_AGENT_ID", ""),
                        help="专家 id；必须显式给出或经 COM_BIANBU_AGENT_ID 传入")
    parser.add_argument("--sessions-root",
                        default=os.path.expanduser("~/.bianbu-agent/sessions"))
    parser.add_argument("--session", default="",
                        help="复用已有会话 id 量一轮（此时不会删除该会话）")
    parser.add_argument("--prompt", default="你好，请用一句话介绍你自己")
    parser.add_argument("--label", default="probe")
    parser.add_argument("--timeout", type=float, default=300.0, help="单轮等待上限（秒）")
    parser.add_argument("--hooks", type=int, default=4, help="每轮打印耗时最长的几个 Hook")
    parser.add_argument("--keep", action="store_true", help="保留探针会话不删除")
    args = parser.parse_args()
    if not args.agent:
        parser.error("缺少 --agent（或设置 COM_BIANBU_AGENT_ID）")

    records = [record for index in range(args.runs) for record in run_session(args, index)]
    for record in records:
        print(json.dumps(record, ensure_ascii=False), flush=True)
    for turn in sorted({record["turn"] for record in records}):
        subset = [record for record in records if record["turn"] == turn]
        for key in ("click_s", "ttft_s", "gap_ms", "api_ms"):
            print(summarise(subset, key, f"{args.label} turn{turn}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
