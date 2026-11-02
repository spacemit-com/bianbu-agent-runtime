#!/usr/bin/env python3
"""Measure creating an Expert and its first Run cold-start latency."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request
import uuid


TERMINAL = ("run/completed", "run/failed", "run/cancelled")
FIRST_TOKEN = (
    "assistant/text.delta",
    "assistant/reasoning.delta",
    "text-chunks",
    "tool/call.started",
)


def request(base: str, method: str, path: str, payload: object = None) -> object:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        body = response.read()
    return None if not body else json.loads(body)


def read_events(path: str) -> list[dict]:
    try:
        raw = subprocess.run(
            ["zstd", "-dc", "-q", path], capture_output=True,
        ).stdout
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
        elif record.get("type") == "text-chunks":
            events.append(record)
    return events


def event_time(event: dict) -> float:
    value = event.get("time0") if event.get("type") == "text-chunks" else event.get("time")
    return float(value if value is not None else event["time"])


def wait_run(path: str, *, deadline: float) -> list[dict]:
    while True:
        events = read_events(path)
        users = [event for event in events if event.get("type") == "user/message"]
        if users:
            run_id = max(users, key=lambda event: int(event.get("seq") or 0)).get("run_id")
            run = [event for event in events if event.get("run_id") == run_id]
            if any(event.get("type") in TERMINAL for event in run):
                return run
        if time.time() >= deadline:
            return run if users else []
        time.sleep(0.1)


def run_once(base: str, sessions_root: str, index: int, timeout: float) -> dict:
    body = {
        "name": f"Perf Expert {index}",
        "name_zh": f"性能专家 {index}",
        "description": "Cold-start latency probe",
        "soul": "Answer briefly.",
        "model": {"provider": "custom:bibit", "name": "deepseek-flash", "effort": "low"},
        "tools": [], "skills": [], "mcps": [],
        "plugins": ["bianbu.bibit.intent", "bianbu.memory", "bianbu.security"],
        "metadata": {},
    }
    started = time.time()
    created = request(base, "POST", "/agents", body)
    create_ms = (time.time() - started) * 1000
    agent_id = str(created["data"]["id"])
    try:
        started = time.time()
        session = request(base, "POST", "/sessions", {
            "agent_id": agent_id, "title": f"expert-start-{index}",
        })
        session_ms = (time.time() - started) * 1000
        session_id = str(session["data"]["id"])
        path = os.path.join(sessions_root, session_id, "session.jsonl.zstd")
        try:
            started = time.time()
            request(base, "POST", f"/sessions/{session_id}/prompt", {
                "message_id": uuid.uuid4().hex,
                "content": "打开终端",
                "attachments": [], "skill_ids": [],
            })
            prompt_ms = (time.time() - started) * 1000
            events = wait_run(path, deadline=time.time() + timeout)
            user = next((event for event in events if event.get("type") == "user/message"), None)
            first_hook = next((event for event in events if event.get("type") == "hook/invoked"), None)
            first_chunk = next(
                (event for event in events if event.get("type") in FIRST_TOKEN), None)
            completed = next((event for event in events if event.get("type") == "run/completed"), None)
            user_time = None if user is None else event_time(user)
            chunk_time = None if first_chunk is None else event_time(first_chunk)
            result = {
                "agent": agent_id,
                "create_ms": round(create_ms, 1),
                "session_ms": round(session_ms, 1),
                "prompt_ms": round(prompt_ms, 1),
                "gap_ms": None if not (user_time is not None and first_hook)
                else round((float(first_hook["time"]) - user_time) * 1000, 1),
                "ttft_s": None if not (user_time is not None and chunk_time is not None)
                else round(chunk_time - user_time, 3),
                "click_s": None if chunk_time is None else round(chunk_time - started, 3),
                "completed_s": None if not (user_time is not None and completed)
                else round(float(completed["time"]) - user_time, 3),
                "terminal": None if completed is None else "run/completed",
                "hooks": [
                    {
                        "plugin": event["data"].get("plugin_id"),
                        "point": event["data"].get("point"),
                        "ms": round(float(event["data"].get("duration_ms") or 0), 1),
                    }
                    for event in events if event.get("type") == "hook/result"
                ],
            }
        finally:
            try:
                request(base, "DELETE", f"/sessions/{session_id}")
            except Exception:
                pass
    finally:
        try:
            request(base, "DELETE", f"/agents/{agent_id}")
        except Exception:
            pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="?", type=int, default=3)
    parser.add_argument("--base", default="http://127.0.0.1:9899/api/v1")
    parser.add_argument(
        "--sessions-root", default=os.path.expanduser("~/.bianbu-agent/sessions"))
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    for index in range(1, args.runs + 1):
        print(json.dumps(
            run_once(args.base, args.sessions_root, index, args.timeout),
            ensure_ascii=False,
        ), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
