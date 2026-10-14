#!/usr/bin/env python3
"""Collect Chinese coreference evidence through the Runtime HTTP API.

This deliberately drives the Runtime HTTP API (not memoryd directly): history
turns are sent as user prompts, then the goldset's current question is sent in
the same or a fresh session.  The report preserves every model answer so a
human can assess semantic correctness; keyword overlap is only a review aid.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def request_json(base: str, method: str, path: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def events(base: str, sid: str, timeout: float) -> list[dict[str, Any]]:
    data = request_json(base, "GET", f"/api/v1/sessions/{sid}/events", None, timeout).get("data") or []
    return data if isinstance(data, list) else []


def create_session(base: str, agent_id: str, title: str, timeout: float) -> str:
    data = request_json(base, "POST", "/api/v1/sessions", {"agent_id": agent_id, "title": title}, timeout)
    return str(data["data"]["id"])


def run_chat(base: str, sid: str, prompt: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError(f"run timeout: {sid}")
        return budget

    receipt = request_json(base, "POST", f"/api/v1/sessions/{sid}/prompt",
        {"message_id": f"coref-acceptance-{uuid.uuid4().hex}", "content": prompt}, remaining()).get("data") or {}
    if not receipt.get("accepted"):
        raise RuntimeError(f"prompt rejected: {receipt}")
    run_id = receipt.get("run_id")
    if not run_id:
        raise RuntimeError("prompt queued without a run_id; use an idle isolated test session")
    while True:
        items = events(base, sid, remaining())
        current = [item for item in items if item.get("run_id") == run_id]
        for item in reversed(current):
            kind = item.get("type")
            payload = item.get("data") or {}
            if kind in {"run/failed", "run/cancelled"}:
                raise RuntimeError(f"{kind}: {payload}")
            if kind == "run/completed":
                status = payload.get("status", "completed")
                if status != "completed":
                    raise RuntimeError(f"run {status}: {payload}")
                return {"content": str(payload.get("content") or ""), "status": status,
                        "run_id": run_id, "events": current}
            if kind == "approval/requested":
                raise RuntimeError("approval requested: acceptance never auto-approves tools; inspect/cancel the test session")
        time.sleep(min(0.4, remaining()))


def answer_text(run: dict[str, Any]) -> str:
    return str(run.get("content") or run.get("reply") or run.get("response") or "")


_ALNUM = re.compile(r"[A-Za-z][A-Za-z0-9_./:+-]*|\d+(?::\d+)?")
_CJK = re.compile(r"[\u3400-\u9fff]+")
_STOP = set("的了呢吗吧啊和与或是有把给被就都也很更一个这那那个这个刚才当前以后先请我你我会将为在不要不要只用下上中及并且方案内容问题对象具体说明回答指代");


def key_terms(text: str) -> list[str]:
    terms: list[str] = []
    terms.extend(x.casefold() for x in _ALNUM.findall(text))
    for run in _CJK.findall(text):
        if len(run) <= 4:
            terms.append(run)
        else:
            # Long labels are matched by informative 2-char chunks.  Drop
            # function words but retain domain nouns (e.g. 版本复盘, 周报).
            terms.extend(run[i : i + 2] for i in range(len(run) - 1)
                         if run[i] not in _STOP and run[i + 1] not in _STOP)
    return list(dict.fromkeys(t for t in terms if t and t not in _STOP))


def classify(case: dict[str, Any], answer: str) -> dict[str, Any]:
    answer_fold = answer.casefold()
    referent = str(case.get("expected_referent") or "")
    action = str(case.get("expected_action_or_answer") or "")
    ref_terms = key_terms(referent)
    action_terms = key_terms(action)
    ref_hits = [term for term in ref_terms if term in answer_fold]
    action_hits = [term for term in action_terms if term in answer_fold]
    ref_ratio = len(ref_hits) / len(ref_terms) if ref_terms else 0.0
    action_ratio = len(action_hits) / len(action_terms) if action_terms else 0.0
    exact = bool(referent and referent.casefold() in answer_fold)
    # Retain the original overlap score only as a triage aid. It cannot
    # distinguish a final choice from a rejected alternative or a synonym.
    passed = bool(answer.strip()) and (exact or (ref_ratio >= 0.6 and action_ratio >= 0.2))
    return {"passed": None, "review_required": True, "heuristic_match": passed, "exact_referent": exact, "referent_ratio": round(ref_ratio, 3),
            "action_ratio": round(action_ratio, 3), "referent_hits": ref_hits,
            "action_hits": action_hits, "ref_terms": ref_terms, "action_terms": action_terms}


def process_case(base_url: str, agent_id: str, case: dict[str, Any], timeout: float) -> dict[str, Any]:
    history = case.get("history") or []
    if any("assistant" in turn for turn in history):
        raise ValueError("history contains assistant fixtures; live prompts cannot reproduce fixed assistant history")
    cid = str(case["id"])
    tags = set(case.get("tags") or [])
    cross = "cross_session" in tags or str(case.get("current_session_id", "")).startswith("new")
    title = f"coref-{cid}-{uuid.uuid4().hex[:8]}"
    old_sid = create_session(base_url, agent_id, title + "-old", timeout) if cross else None
    sid = create_session(base_url, agent_id, title, timeout)
    history_runs = []
    for turn in case.get("history") or []:
        target = old_sid if cross else sid
        history_runs.append({"user": str(turn.get("user") or ""), "run": run_chat(base_url, target, str(turn.get("user") or ""), timeout)})
    question = (
        "This is a read-only semantic coreference acceptance test. Do not call tools or modify the system.\n"
        + str(case.get("current_question") or "")
        + "\nIdentify exactly what each pronoun or ordinal in the question refers to, then answer the question."
    )
    final = run_chat(base_url, sid, question, timeout)
    answer = answer_text(final)
    verdict = classify(case, answer)
    return {"id": cid, "category": case.get("category"), "tags": case.get("tags"),
        "cross_session": cross, "session_id": sid, "old_session_id": old_sid,
        "history_count": len(history_runs), "history_runs": history_runs, "final_run": final, "question": case.get("current_question"),
        "expected_referent": case.get("expected_referent"), "expected_action_or_answer": case.get("expected_action_or_answer"),
        "answer": answer, **verdict}


def load_cases(path: Path, limit: int | None, offset: int = 0) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = rows[offset:]
    return rows[:limit] if limit else rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:9899")
    ap.add_argument("--agent-id", required=True, help="Isolated test Agent; history prompts may invoke its tools/hooks")
    ap.add_argument("--goldset", type=Path, required=True)
    ap.add_argument("--output", type=Path, default=Path("coreference-runtime-acceptance.json"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()
    cases = load_cases(args.goldset, args.limit, args.offset)
    report: dict[str, Any] = {"status": "COREFERENCE_RUNTIME_EVIDENCE", "spec": "MEM-INT-08-02",
        "base_url": args.base_url, "agent_id": args.agent_id, "offset": args.offset,
        "cases": len(cases), "results": []}
    started = time.monotonic()
    # Sequential collection avoids concurrent cases racing shared recent-memory order.
    # It does not isolate persistent memory: provision a dedicated identity externally.
    report["workers"] = 1
    for case in cases:
        try:
            row = process_case(args.base_url, args.agent_id, case, args.timeout)
        except Exception as exc:
            row = {"id": case["id"], "passed": None, "review_required": True,
                   "execution_error": f"{type(exc).__name__}: {exc}"}
        report["results"].append(row)
        print(f"[{len(report['results'])}/{len(cases)}] {row['id']}: evidence collected; review required", flush=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["results"].sort(key=lambda row: row["id"])
    report["heuristic_matches"] = sum(bool(row.get("heuristic_match")) for row in report["results"])
    report["execution_errors"] = sum("execution_error" in row for row in report["results"])
    report["accuracy"] = None
    report["threshold"] = 0.80
    report["threshold_pass"] = None
    report["review_required"] = True
    report["limitations"] = ["Keyword overlap is not semantic correctness", "Persistent memory identity is not isolated by creating a session", "History prompts may execute tools/hooks; use an isolated test environment"]
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "cases", "heuristic_matches", "execution_errors", "accuracy", "threshold", "threshold_pass", "elapsed_seconds")}, ensure_ascii=False, indent=2))
    return 2  # Evidence collection never claims semantic acceptance without review.


if __name__ == "__main__":
    raise SystemExit(main())
