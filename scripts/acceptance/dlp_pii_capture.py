"""Prove that a safe-mode Agent never sends real PII to its model provider.

The script creates (or reuses) an Agent wired to a recording provider, sends
three turns that mention the same people, and then scans every captured
request body for the real values.  It also asserts the user-facing stream and
terminal result still contain the real values.

Usage (on the K3 board, where the Runtime listens on 9899)::

    python3 scripts/acceptance/dlp_capture_provider.py 19900 &   # recording provider
    curl -s -X POST http://127.0.0.1:9899/api/v1/providers -H 'Content-Type: application/json' \\
        -d '{"id":"capture","label":"capture","base_url":"http://127.0.0.1:19900/v1","api_key":"x","parameters":{"record_kind":"custom_provider"}}'
    python3 scripts/acceptance/dlp_pii_capture.py capture

A new Agent defaults to ``yolo``.  The script switches it to ``safe`` before
sending anything; without that step every turn returns
``decision=ALLOW, mode=yolo, dlp_scanned=false`` and the check would measure
nothing.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:9899"
LOG = "/tmp/provider_requests.jsonl"

REAL = ["林嘉禾", "周若彤", "13812345678", "alice@example.com"]
TURNS = [
    "请记住：我叫林嘉禾，我父亲叫周若彤，手机号 13812345678，"
    "邮箱 alice@example.com。只回复 OK。",
    "请完整复述第一轮我告诉你的两个姓名和手机号。",
    "我父亲的名字是周若彤还是别人？只回答姓名。",
]


def api(path: str, body=None, method=None):
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        BASE + path, data=payload, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{method or 'GET'} {path} -> {exc.code}: "
            f"{exc.read().decode('utf-8', 'replace')[:400]}"
        ) from exc


def security(operation: str, **params):
    response = api("/security-api/invoke", {
        "protocolVersion": 1, "requestId": uuid.uuid4().hex,
        "serviceId": "security", "operationId": operation, "params": params,
    })
    if not response.get("ok"):
        raise RuntimeError(
            f"{operation} failed: {json.dumps(response.get('error'), ensure_ascii=False)}"
        )
    return response["result"]


def events(session_id: str):
    return api(f"/api/v1/sessions/{session_id}/events?from_seq=0")["data"]


def run_turn(session_id: str, text: str, tag: str):
    start = max((event.get("seq", 0) for event in events(session_id)), default=0)
    api(f"/api/v1/sessions/{session_id}/prompt", {
        "message_id": "m-" + tag, "content": text, "attachments": [],
    })
    deadline = time.time() + 300
    approved: set[str] = set()
    run_id = None
    while time.time() < deadline:
        page = [event for event in events(session_id)
                if event.get("seq", 0) > start]
        for event in page:
            if event.get("type") == "user/message":
                run_id = run_id or event.get("run_id")
            if event.get("type") == "approval/requested":
                approval_id = (event.get("data") or {}).get("approval_id")
                if approval_id and approval_id not in approved:
                    approved.add(approval_id)
                    api(f"/api/v1/sessions/{session_id}/approvals/{approval_id}",
                        {"decision": "approve"})
        if run_id and any(event.get("type") == "run/completed"
                          and event.get("run_id") == run_id for event in page):
            return page
        if any(event.get("type") in {"run/failed", "run/cancelled"}
               for event in page):
            return page
        time.sleep(1.5)
    raise TimeoutError("run did not finish")


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else "capture"
    agent_id = sys.argv[2] if len(sys.argv) > 2 else ""
    if not agent_id:
        agent = api("/api/v1/agents", {
            "name": "DLP Capture " + uuid.uuid4().hex[:6],
            "model": {"provider": provider, "name": "capture"},
            "tools": ["read_file"],
            "plugins": ["bianbu.security"],
        })["data"]
        agent_id = agent["id"]
    print("AGENT:", agent_id)

    mode = security("agents.security-mode.get.v1", agent_id=agent_id)
    if mode.get("mode") != "safe":
        security("agents.security-mode.update.v1",
                 agent_id=agent_id, mode="safe")
        mode = security("agents.security-mode.get.v1", agent_id=agent_id)
    print("MODE:", mode.get("mode"))
    if mode.get("mode") != "safe":
        print("RESULT: INCONCLUSIVE (Agent is not in safe mode)")
        return 2

    session_id = api("/api/v1/sessions", {
        "agent_id": agent_id, "title": "dlp-capture",
    })["data"]["id"]
    print("SESSION:", session_id)
    open(LOG, "w").close()

    terminal: list[str] = []
    for index, text in enumerate(TURNS, 1):
        page = run_turn(session_id, text, f"{uuid.uuid4().hex[:6]}-{index}")
        content = next((event["data"].get("content", "") for event in page
                        if event.get("type") == "run/completed"), "")
        terminal.append(content)
        print(f"TURN {index} terminal:", content[:160].replace("\n", " | "))

    requests = []
    with open(LOG, encoding="utf-8") as stream:
        for line in stream:
            requests.append(json.loads(line))
    print("\nCAPTURED REQUESTS:", len(requests))

    leaks = 0
    for number, item in enumerate(requests, 1):
        body = json.dumps(item["body"], ensure_ascii=False)
        found = [value for value in REAL if value in body]
        print(f"  req{number} path={item['path']} bytes={len(body)} real={found}")
        leaks += len(found)

    visible = sum(1 for value in REAL if value in "".join(terminal))
    print("\nREAL-VALUE LEAKS TO PROVIDER:", leaks)
    print("REAL VALUES RETURNED TO USER:", visible, "of", len(REAL))
    ok = leaks == 0 and len(requests) == len(TURNS)
    print("RESULT:", "PASS" if ok else "FAIL")
    print("SESSION_ID:", session_id, "AGENT_ID:", agent_id)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
