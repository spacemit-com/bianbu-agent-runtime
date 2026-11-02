"""Product-facing audit view: five categories and plain-language titles."""
from __future__ import annotations

import json

import pytest

from bianbu_agent.security.audit_view import (
    build_audit_view,
    build_view_record,
    query_audit_view,
)


def record(op_type: str, target: str = "", result: str = "success",
           *, agent_id: str = "com.bianbu.demo", ts: str = "2026-10-23T09:00:00+00:00",
           seq: int = 1) -> dict:
    return {"seq": seq, "ts": ts, "agent_id": agent_id,
            "op_type": op_type, "target": target, "result": result}


@pytest.mark.parametrize(("op_type", "target", "result", "category"), [
    ("bind_caller", ":1.20141", "success", "identity"),
    ("caller_identity", ":1.99", "deny", "identity"),
    ("check_permission", "write_file:~/Documents", "ask", "decision"),
    ("ask", "write_file", "ask_approved", "confirmation"),
    ("confirm", "terminal", "confirm_approved;mode=yolo", "confirmation"),
    ("session_trust", "read_file", "grant", "confirmation"),
    ("set_policy", "write_file", "allow:ok", "change"),
    ("security_mode_change", "old_mode=sandbox;new_mode=yolo", "success", "change"),
    ("build_sandbox_policy", "com.bianbu.demo", "success", "anomaly"),
    ("register_agent", "com.bianbu.demo", "success", "identity"),
    ("dlp_bypass", "model_call", "mode=yolo;scanned=false", "decision"),
    ("hook:agent_shutdown", "ALLOW", "dlp_shutdown@0.3=ALLOW", "anomaly"),
    ("hook:pre_tool_call", "CONFIRM", "sec_capability@3.5=CONFIRM", "decision"),
    ("unknown.future_event", "x", "success", "anomaly"),
])
def test_categories(op_type, target, result, category):
    view = build_view_record(record(op_type, target, result))
    assert view["category"] == category


@pytest.mark.parametrize(("op_type", "target", "result", "title_part", "outcome", "severity"), [
    ("check_permission", "write_file:~/Documents", "allow", "被允许使用 write_file", "allowed", "info"),
    ("check_permission", "terminal:", "deny", "被拒绝使用 terminal", "denied", "critical"),
    ("check_permission", "terminal:", "ask", "等待你确认", "needs_confirmation", "warning"),
    ("ask", "terminal", "ask_denied", "你拒绝了", "rejected", "warning"),
    ("ask", "write_file", "ask_approved", "你批准了", "approved", "warning"),
    ("caller_identity", ":1.99", "deny", "试图冒充", "denied", "critical"),
    ("hook:agent_shutdown", "ALLOW", "dlp_shutdown@0.3=ALLOW",
     "结束了一次会话", "allowed", "info"),
    ("hook:pre_tool_call", "pre_tool_call",
     "decision=ALLOW;mode=yolo;checks=skipped", "跳过了工具调用前检查",
     "allowed", "info"),
    ("dlp_bypass", "model_call", "mode=yolo;scanned=false",
     "跳过了模型调用的内容检查", "info", "info"),
    ("register_agent", "com.bianbu.demo", "success",
     "刷新了安全身份登记", "success", "info"),
    ("build_sandbox_policy", "com.bianbu.demo", "success",
     "生成了受限运行环境", "success", "info"),
    ("set_policy", "write_file", "allow:ok", "规则被改为 allow", "success", "warning"),
    ("security_mode_change", "old_mode=sandbox;new_mode=yolo", "success",
     "从 sandbox 改为 yolo", "success", "warning"),
    ("unknown.future_event", "x", "success", "其他安全事件", "success", "info"),
    ("terminal", "executed", "ok", "执行了 terminal", "allowed", "info"),
    ("terminal", "unmapped.tool:", "unmapped:deny",
     "未声明的工具 terminal", "denied", "critical"),
    ("error:read_file", "on_error", "permission denied",
     "执行出错", "failed", "warning"),
])
def test_titles_outcomes_and_severity(op_type, target, result, title_part,
                                      outcome, severity):
    view = build_view_record(record(op_type, target, result))
    assert title_part in view["title"]
    assert view["outcome"] == outcome
    assert view["severity"] == severity


def test_attention_hides_routine_allow_but_keeps_denials_and_changes():
    allow = build_view_record(record("check_permission", "read_file:", "allow"))
    deny = build_view_record(record("check_permission", "process:", "deny"))
    change = build_view_record(record("set_policy", "write_file", "allow:ok"))
    bind = build_view_record(record("bind_caller", ":1.2", "success"))
    assert allow["attention"] is False
    assert deny["attention"] is True
    assert change["attention"] is True
    assert bind["attention"] is False


def test_group_key_buckets_events_five_seconds_apart_by_time():
    first = build_view_record(record("ask", "think", "ask_denied",
                                     ts="2026-10-23T09:00:01+00:00", seq=1))
    second = build_view_record(record("check_permission", "read_file:", "allow",
                                      ts="2026-10-23T09:00:02+00:00", seq=2))
    later = build_view_record(record("ask", "think", "ask_denied",
                                     ts="2026-10-23T09:00:09+00:00", seq=3))
    assert first["group_key"] == second["group_key"]
    assert later["group_key"] != first["group_key"]


def test_build_audit_view_preserves_pagination_and_summarizes():
    page = {
        "records": [
            record("check_permission", "read_file:", "allow", seq=1),
            record("check_permission", "process:", "deny", seq=2),
            record("ask", "write_file", "ask_approved", seq=3),
            record("set_policy", "write_file", "deny:ok", seq=4),
        ],
        "snapshot_seq": 10,
        "has_more": True,
        "next_cursor": 4,
    }
    view = build_audit_view(page)
    assert view["snapshot_seq"] == 10
    assert view["has_more"] is True
    assert view["next_cursor"] == 4
    assert view["summary"]["total"] == 4
    assert view["summary"]["attention"] == 3
    assert view["summary"]["by_category"]["decision"] == 2
    assert view["summary"]["by_category"]["confirmation"] == 1
    assert view["summary"]["by_severity"]["critical"] == 1


def test_build_audit_view_hides_retired_network_learning_rows():
    page = {
        "records": [
            record("network_learning_event", "api.example.com:443", "success", seq=3),
            record("network_learning_status", "active", "success", seq=2),
            record("check_permission", "terminal:", "deny", seq=1),
        ],
        "snapshot_seq": 10,
        "has_more": False,
        "next_cursor": None,
    }
    view = build_audit_view(page)
    assert [row["op_type"] for row in view["records"]] == ["check_permission"]
    assert view["summary"]["total"] == 1
    assert view["summary"]["by_category"]["anomaly"] == 0


def test_query_audit_view_skips_retired_network_learning_rows_across_pages():
    rows = [record("network_learning_event", f"host-{seq}:443", "success", seq=seq)
            for seq in range(2, 8)]
    rows.append(record("check_permission", "terminal:", "deny", seq=1))
    fetch, calls = fake_source(rows)
    view = query_audit_view(fetch, {"limit": 5}, chunk=3)
    assert [row["seq"] for row in view["records"]] == [1]
    assert view["summary"]["total"] == 1
    assert view["summary"]["scanned"] == 7
    assert len(calls) == 3


def test_technical_fields_are_preserved_for_the_raw_view():
    view = build_view_record(record("check_permission", "terminal:", "deny", seq=42))
    assert view["technical"] == {
        "seq": 42, "op_type": "check_permission",
        "target": "terminal:", "result": "deny",
    }
    assert view["seq"] == 42
    assert view["target"] == "terminal:"


def fake_source(rows):
    """A bounded newest-first source with securityd's cursor semantics."""

    calls = []

    def fetch(query):
        calls.append(dict(query))
        snapshot = query.get("snapshot_seq", 10**9)
        cursor = query["cursor"]
        upper = min(snapshot, cursor - 1) if cursor else snapshot
        matching = sorted((row for row in rows if row["seq"] <= upper),
                          key=lambda row: row["seq"], reverse=True)
        limit = query["limit"]
        page = matching[:limit + 1]
        has_more = len(page) > limit
        records = page[:limit]
        return {"records": records, "snapshot_seq": snapshot,
                "has_more": has_more,
                "next_cursor": records[-1]["seq"] if has_more else None}

    return fetch, calls


def test_query_audit_view_filters_attention_across_pages():
    rows = [record("check_permission", "read_file:", "allow", seq=seq)
            for seq in range(2, 12)]
    rows.append(record("check_permission", "terminal:", "deny", seq=1))
    fetch, calls = fake_source(rows)
    view = query_audit_view(fetch, {"limit": 5, "attention": True}, chunk=4)
    assert len(calls) == 3
    assert [row["seq"] for row in view["records"]] == [1]
    assert view["has_more"] is False
    assert view["summary"]["scanned"] == 11
    assert view["records"][0]["severity"] == "critical"


def test_query_audit_view_keeps_cursor_on_last_returned_match():
    rows = [record("check_permission", "read_file:", "allow", seq=seq)
            for seq in range(1, 9)]
    rows += [record("check_permission", "terminal:", "deny", seq=9),
             record("check_permission", "process:", "deny", seq=10)]
    fetch, _ = fake_source(rows)
    view = query_audit_view(fetch, {"limit": 1, "category": "decision",
                                    "outcome": "denied"})
    assert [row["seq"] for row in view["records"]] == [10]
    assert view["has_more"] is True
    assert view["next_cursor"] == 10
    rest = query_audit_view(fetch, {"limit": 1, "category": "decision",
                                    "outcome": "denied",
                                    "cursor": view["next_cursor"],
                                    "snapshot_seq": view["snapshot_seq"]})
    assert [row["seq"] for row in rest["records"]] == [9]
    assert rest["has_more"] is False


def test_query_audit_view_scan_bound_makes_progress_without_matches():
    rows = [record("check_permission", "read_file:", "allow", seq=seq)
            for seq in range(1, 7)]
    fetch, calls = fake_source(rows)
    view = query_audit_view(fetch, {"limit": 5, "category": "identity"},
                            chunk=2, max_scan=4)
    assert view["records"] == []
    assert view["has_more"] is True
    assert view["next_cursor"] == 3
    assert len(calls) == 2


def test_query_audit_view_rejects_unknown_category():
    fetch, _ = fake_source([])
    try:
        query_audit_view(fetch, {"category": "unknown"})
    except ValueError as error:
        assert "category" in str(error)
    else:
        raise AssertionError("unknown category must be rejected")


def test_outcome_group_combines_deny_and_user_rejection():
    rows = [record("check_permission", "terminal:", "deny", seq=2),
            record("ask", "terminal", "ask_denied", seq=1)]
    fetch, _ = fake_source(rows)
    view = query_audit_view(fetch, {"outcome_group": "blocked"})
    assert [row["seq"] for row in view["records"]] == [2, 1]
    assert {row["outcome"] for row in view["records"]} == {"denied", "rejected"}


def test_historical_batch_payload_is_redacted_and_not_attention():
    raw = json.dumps({"entries": [{"payload": "secret chat content"}]})
    view = build_view_record(record("uds_audit_batch", "entries=1", raw))
    assert view["result"] == "historical_payload_redacted"
    assert view["technical"]["result"] == "historical_payload_redacted"
    assert view["outcome"] == "success"
    assert view["attention"] is False
    assert "secret chat content" not in json.dumps(view, ensure_ascii=False)


def test_dict_scope_is_hidden_from_the_plain_language_title():
    view = build_view_record(record(
        "check_permission", "terminal:{'session_id': 'session-1'}", "ask"))
    assert "等待你确认" in view["title"]
    assert "session_id" not in view["title"]


def test_operator_rows_are_not_part_of_the_product_audit_view():
    rows = [
        record("admin:respond_confirm", "actor=uid=1000;approved=False", "success", seq=1),
        record("check_permission", "terminal:", "ask", seq=2),
    ]
    view = build_audit_view({"records": rows})

    assert [row["seq"] for row in view["records"]] == [2]
    assert "actor" not in view["summary"]["by_category"]
    with pytest.raises(ValueError):
        query_audit_view(lambda raw: {"records": rows, "snapshot_seq": 2,
                                      "has_more": False, "next_cursor": None},
                         {"category": "actor"})
