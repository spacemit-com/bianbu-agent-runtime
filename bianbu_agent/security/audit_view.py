"""Turn raw securityd audit rows into a user-facing audit view.

The stored row remains the source of truth.  This module only adds a stable
category, a plain-language sentence, an outcome, a severity and a presentation
grouping key.  It never rewrites ``op_type``/``target``/``result``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

CATEGORY_LABELS = {
    "identity": "谁接入了",
    "decision": "想做什么",
    "confirmation": "征求过你的同意",
    "actor": "谁操作的",
    "change": "设置被改了",
    "anomaly": "出过什么问题",
}
CATEGORY_ORDER = tuple(CATEGORY_LABELS)

# Operator actions (``admin:*``) are console bookkeeping, not Agent behaviour.
# They stay in the securityd audit chain for compliance but are not part of the
# product-facing list the user reads, so their category is not presented.
HIDDEN_CATEGORIES = frozenset({"actor"})
PUBLIC_CATEGORY_ORDER = tuple(
    name for name in CATEGORY_ORDER if name not in HIDDEN_CATEGORIES
)

OUTCOME_LABELS = {
    "allowed": "允许",
    "success": "成功",
    "approved": "已批准",
    "rejected": "已拒绝",
    "denied": "已拒绝",
    "needs_confirmation": "待确认",
    "failed": "失败",
    "skipped": "已跳过",
    "info": "记录",
}

# The UI groups outcomes instead of exposing raw enum values: a user asking
# "what was refused" wants both an explicit deny and their own rejection.
OUTCOME_GROUPS = {
    "allowed": "ok",
    "success": "ok",
    "approved": "approved",
    "rejected": "blocked",
    "denied": "blocked",
    "needs_confirmation": "pending",
    "failed": "failed",
    "skipped": "ok",
    "info": "ok",
}

SEVERITY_LABELS = {
    "critical": "需要处理",
    "warning": "请留意",
    "info": "正常",
}

_TOOL_NAMES = {
    "read_file", "write_file", "search_files", "terminal", "process",
    "think", "e2e_test",
}
_CONFIRMATION_OPS = {"ask", "confirm", "session_trust"}
_CHANGE_OPS = {
    "register", "unregister_agent", "install",
    "sync_capabilities", "set_policy", "set_permission_group",
    "security_mode_change", "set_global_syscall_policy",
    "set_agent_syscall_policy", "reset_agent_syscall_policy",
    "network_policy", "revoke", "suspend",
}
_ANOMALY_OPS = {
    "build_sandbox_policy", "build_seccomp_profile",
    "confirm_cancelled_shutdown",
}
_HOOK_LIFECYCLE = {"agent_startup", "agent_shutdown"}
_HOOK_STAGE_LABELS = {
    "input": "输入",
    "pre_model_call": "模型调用前",
    "post_model_call": "模型调用后",
    "pre_tool_call": "工具调用前",
    "post_tool_call": "工具调用后",
    "output": "输出",
}
_HOOK_DECISIONS = {"ALLOW", "BLOCK", "CONFIRM", "MODIFY"}
_ADMIN_ACTIONS = {
    "register_capabilities": "更新了 Agent 的能力声明",
    "reset_agent_binding": "清除了 Agent 的调用者绑定",
    "set_network_policy": "更新了 Agent 的网络策略",
    "suspend_agent": "暂停了 Agent",
}

# Network learning was removed.  Devices upgraded from older releases may
# still have its observation rows in the audit database; keep those internal
# rows out of the product-facing view instead of showing them as unknown
# security events.
_RETIRED_OP_PREFIX = "network_learning_"


def _visible(record: Mapping[str, Any]) -> bool:
    op = str(record.get("op_type", ""))
    return not (op.startswith(_RETIRED_OP_PREFIX) or op.startswith("admin:"))


def categorize(op_type: object) -> str:
    """Map a raw op_type to one of the six product categories."""
    op = str(op_type or "")
    if op in {"bind_caller", "caller_identity", "register_agent"}:
        return "identity"
    if op.startswith("admin:"):
        return "actor"
    if op in _CONFIRMATION_OPS:
        return "confirmation"
    if op.startswith("hook:"):
        return "anomaly" if op[5:] in _HOOK_LIFECYCLE else "decision"
    if op.startswith("hook_payload:") or op == "uds_audit_batch":
        return "decision"
    if op in {"check_permission", "uds_dlp_scan", "dlp_bypass"} or op in _TOOL_NAMES:
        return "decision"
    if op.startswith("error:"):
        return "anomaly"
    if op in _CHANGE_OPS or op.startswith("set_"):
        return "change"
    if op.startswith("runtime.") or op in _ANOMALY_OPS:
        return "anomaly"
    # Unknown events stay visible instead of being silently hidden.
    return "anomaly"


def _capability_scope(target: str) -> tuple[str, str]:
    capability, _, scope = target.partition(":")
    return capability, scope


def _scope_hint(scope: str) -> str:
    """Show a useful scope, never a dict dump or an unbounded payload."""
    text = str(scope or "").strip().replace("\n", " ")
    if not text or text.startswith(("{", "[")):
        return ""
    return f"（{text[:80]}{'…' if len(text) > 80 else ''}）"


def _safe_result(op: str, result: str) -> str:
    """Keep historical audit content out of the product-facing view."""
    if op == "uds_audit_batch" or op.startswith("hook_payload:"):
        if "content_fingerprint" not in result:
            return "historical_payload_redacted"
    return result


def _kv(target: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in target.split(";"):
        key, sep, value = part.partition("=")
        if sep and key and key not in out:
            out[key] = value
    return out


def _actor_label(target: str) -> str:
    fields = _kv(target)
    uid = fields.get("actor", "")
    if not uid and "uid" in fields:
        uid = fields["uid"]
    return f"管理员（{uid}）" if uid else "管理员"


def _agent(agent_id: object) -> str:
    value = str(agent_id or "").strip()
    return value or "未知 Agent"


def _hook_decision(target: str, result: str) -> str:
    """Return the decision for both audit shapes.

    securityd writes the decision in ``target``; the Runtime's YOLO shortcut
    writes the stage in ``target`` and ``decision=...`` in ``result``.
    """
    decision = target.strip().upper()
    if decision in _HOOK_DECISIONS:
        return decision
    for part in result.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "decision":
            return value.strip().upper()
    return decision


def _outcome(op: str, target: str, result: str, category: str) -> str:
    if op == "uds_audit_batch" or op.startswith("hook_payload:"):
        return "success"
    if op.startswith("hook:"):
        decision = _hook_decision(target, result)
        if decision == "ALLOW":
            return "allowed"
        if decision == "CONFIRM":
            return "needs_confirmation"
        if decision == "BLOCK":
            return "denied"
    text = f"{target};{result}".lower()
    if "approved=false" in text or "ask_denied" in text or "rejected" in text:
        return "rejected"
    if "deny" in text or "block" in text:
        return "denied"
    if result.strip().lower() == "ask":
        return "needs_confirmation"
    if "confirm" in target.lower() and "approv" not in text:
        return "needs_confirmation"
    if "approved" in text or "grant" in text:
        return "approved"
    if "skipped" in text:
        return "skipped"
    if any(word in text for word in ("fail", "error", "timeout", "cancel")):
        return "failed"
    if any(word in text for word in ("allow", "success", "executed", "applied", "ok")):
        return "allowed" if category == "decision" else "success"
    return "info"


def _severity(category: str, outcome: str, op: str, text: str) -> str:
    lowered = text.lower()
    if op in {"caller_identity", "runtime.policy_mismatch", "runtime.worker_crash"}:
        return "critical"
    if "audit_failure" in lowered or "audit_failed" in lowered:
        return "critical"
    if outcome in {"denied", "rejected", "failed"}:
        return "critical" if category in {"identity", "decision"} else "warning"
    if outcome == "needs_confirmation":
        return "warning"
    if category == "confirmation" and outcome in {"approved", "rejected"}:
        return "warning"
    if category in {"change", "actor"}:
        return "warning"
    return "info"


def _title(op: str, category: str, agent: str, target: str, result: str) -> str:
    capability, scope = _capability_scope(target)
    scope_hint = _scope_hint(scope)
    if op == "bind_caller":
        return f"{agent} 接入了系统"
    if op == "caller_identity":
        return f"有程序试图冒充 {agent}，已被拒绝"
    if op == "check_permission":
        lowered = result.lower()
        if "mode=yolo" in lowered:
            return f"{agent} 在正常模式下跳过了 {capability} 的检查"
        if lowered == "allow":
            return f"{agent} 被允许使用 {capability}{scope_hint}"
        if lowered == "ask":
            return f"{agent} 请求使用 {capability}{scope_hint}，等待你确认"
        if lowered == "deny":
            return f"{agent} 被拒绝使用 {capability}{scope_hint}"
        return f"{agent} 的 {capability}{scope_hint} 判定为 {result or '未知'}"
    if op == "uds_dlp_scan":
        lowered = result.lower()
        if "mode=yolo" in lowered:
            return f"{agent} 在正常模式下跳过了内容检查"
        fields = _kv(result)
        found = fields.get("findings")
        if found and found not in {"0", ""}:
            return f"{agent} 的内容检查发现 {found} 处敏感信息"
        return f"{agent} 的内容检查完成，未发现敏感信息"
    if op == "dlp_bypass":
        label = {"model_call": "模型调用", "skill_text": "Skill 文本"}.get(
            target, target or "内容")
        return f"{agent} 在正常模式下跳过了{label}的内容检查"
    if op.startswith("hook:"):
        stage = op[5:]
        if stage == "agent_startup":
            if _hook_decision(target, result) == "BLOCK":
                return f"{agent} 的启动请求被拒绝"
            return f"{agent} 开始了一次会话"
        if stage == "agent_shutdown":
            if _hook_decision(target, result) == "BLOCK":
                return f"{agent} 的收尾输出被拒绝，会话已结束"
            return f"{agent} 结束了一次会话，敏感信息映射已清理"
        label = _HOOK_STAGE_LABELS.get(stage, stage)
        if "checks=skipped" in result.lower():
            return f"{agent} 在正常模式下跳过了{label}检查"
        decision = _hook_decision(target, result)
        if decision == "CONFIRM":
            return f"{agent} 的{label}检查需要你确认"
        if decision == "BLOCK":
            return f"{agent} 的{label}检查被拒绝"
        if decision == "MODIFY":
            return f"{agent} 的{label}检查修改了内容"
        if decision == "ALLOW":
            return f"{agent} 的{label}检查完成"
        return f"{agent} 的{label}检查结果为 {target or '未知'}"
    if op in _TOOL_NAMES:
        lowered = f"{target};{result}".lower()
        if "unmapped:" in lowered:
            policy = result.split(":", 1)[-1]
            label = {"allow": "放行", "confirm": "要求你确认",
                     "deny": "拒绝"}.get(policy, policy)
            return f"{agent} 使用了未声明的工具 {op}，已按“{label}”处理"
        if "deny" in lowered or "block" in lowered:
            return f"{agent} 使用 {op} 被拒绝"
        if target == "executed" or result == "ok":
            return f"{agent} 执行了 {op}"
        if "executed" in lowered or "success" in lowered:
            return f"{agent} 执行了 {op}"
        if "allow" in lowered:
            return f"{agent} 使用了 {op}"
        return f"{agent} 的 {op} 操作结果为 {result or '未知'}"
    if op == "ask":
        lowered = result.lower()
        if "approved" in lowered and "denied" not in lowered:
            return f"你批准了 {agent} 使用 {capability}"
        if "denied" in lowered or "rejected" in lowered:
            return f"你拒绝了 {agent} 使用 {capability}"
        return f"{agent} 的 {capability} 确认请求已处理"
    if op == "confirm":
        return f"{agent} 在正常模式下自动放行了 {capability}"
    if op == "session_trust":
        if "grant" in result.lower():
            return f"{agent} 在本次会话中获得了 {capability} 的信任"
        return f"{agent} 的 {capability} 会话信任申请被拒绝"
    if op.startswith("admin:"):
        admin = _actor_label(target)
        fields = _kv(target)
        if op == "admin:respond_confirm":
            if "approved=false" in target.lower():
                return f"{admin}拒绝了一次确认请求"
            return f"{admin}批准了一次确认请求"
        if op == "admin:revoke_permission":
            tool = fields.get("tool", "权限")
            return f"{admin}收回了 {fields.get('agent', agent)} 的 {tool} 权限"
        if op == "admin:bind_agent":
            return f"{admin}重新绑定了 {fields.get('agent', agent)} 的调用者身份"
        return f"{admin}{_ADMIN_ACTIONS.get(op[6:], '执行了 ' + op[6:].replace('_', ' '))}"
    if op == "set_policy":
        fields = _kv(result)
        policy = next(iter(fields), result.split(":", 1)[0] or "未知")
        return f"{agent} 的 {capability} 规则被改为 {policy}"
    if op == "set_permission_group":
        group = target.split(":", 1)[0] or "权限"
        return f"{agent} 的 {group} 权限被修改"
    if op == "security_mode_change":
        fields = _kv(target)
        return (f"{agent} 的安全模式从 {fields.get('old_mode', '?')} "
                f"改为 {fields.get('new_mode', '?')}")
    if op in {"set_global_syscall_policy", "set_agent_syscall_policy"}:
        version = result.rsplit(":", 1)[-1] if ":" in result else "?"
        scope = "全局" if op.startswith("set_global") else agent
        return f"{scope}的系统调用黑名单更新到版本 {version}"
    if op == "reset_agent_syscall_policy":
        return f"{agent} 的系统调用黑名单已恢复默认"
    if op == "network_policy":
        applied = {
            "applied": "已生效",
            "pending_launch": "下次启动生效",
            "artifact_only:enforcement_unavailable": "已保存，但当前无法强制",
        }.get(result, result or "已保存")
        return f"{agent} 的网络策略{applied}"
    if op == "register_agent":
        return f"{agent} 刷新了安全身份登记"
    if op == "register":
        return f"{agent} 注册了安全身份"
    if op == "unregister_agent":
        return f"{agent} 被注销"
    if op == "install":
        return f"{agent} 安装了 {target or '一个包'}"
    if op == "sync_capabilities":
        return f"{agent} 的能力声明已同步"
    if op == "revoke":
        return f"{agent} 的 {capability} 权限被收回"
    if op == "suspend":
        return f"{agent} 被暂停"
    if op == "build_sandbox_policy":
        return f"{agent} 生成了受限运行环境的沙箱和网络规则"
    if op == "build_seccomp_profile":
        fields = _kv(target)
        return (f"{agent} 生成了系统调用过滤策略"
                f"（{fields.get('arch', '?')}，版本 {fields.get('version', '?')}）")
    if op.startswith("runtime."):
        labels = {
            "runtime.worker_spawn": "启动了运行进程",
            "runtime.worker_hello": "的运行进程已就绪",
            "runtime.worker_stop": "的运行进程已停止",
            "runtime.worker_crash": "的运行进程异常退出",
            "runtime.policy_mismatch": "的运行策略与预期不一致",
            "runtime.orphan_reaped": "的残留进程已清理",
        }
        return f"{agent} {labels.get(op, '的运行时事件：' + op[8:])}"
    if op == "confirm_cancelled_shutdown":
        return "服务关闭时取消了一次确认请求"
    if op == "uds_audit_batch":
        return f"{agent} 记录了一批内容检查摘要（不含原文）"
    if op.startswith("hook_payload:"):
        stage = op.split(":", 1)[1]
        label = _HOOK_STAGE_LABELS.get(stage, stage)
        return f"{agent} 记录了{label}的内容检查摘要（不含原文）"
    if op.startswith("error:"):
        tool = op.split(":", 1)[1] or "工具"
        detail = result[:80] + ("…" if len(result) > 80 else "")
        return f"{agent} 的 {tool} 执行出错：{detail or '未知原因'}"
    return f"其他安全事件：{op or '未知'}"


def _object(op: str, agent_id: str, target: str) -> dict[str, str]:
    capability, scope = _capability_scope(target)
    if op in {"check_permission", "ask", "confirm", "session_trust",
              "set_policy", "revoke"}:
        return {"type": "tool", "id": capability, "scope": scope}
    if op in {"bind_caller", "caller_identity"}:
        return {"type": "caller", "id": target}
    if op.startswith("admin:"):
        fields = _kv(target)
        return {"type": "admin", "id": fields.get("request_id", op[6:])}
    if op.startswith("hook:"):
        return {"type": "agent", "id": agent_id, "scope": op[5:]}
    if op in _TOOL_NAMES:
        return {"type": "tool", "id": target.split(":", 1)[0] or op}
    if op.startswith("runtime.") or op.startswith("build_"):
        return {"type": "worker", "id": target or agent_id}
    if op in {"set_global_syscall_policy", "set_agent_syscall_policy",
              "reset_agent_syscall_policy"}:
        return {"type": "syscall_policy", "id": target}
    if op == "network_policy":
        return {"type": "network", "id": target}
    return {"type": "event", "id": op}


def _group_key(ts: str) -> str:
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return f"g{int(parsed.timestamp() // 5)}"
    except (TypeError, ValueError):
        return f"g{ts}"


def build_view_record(record: Mapping[str, Any]) -> dict[str, Any]:
    op = str(record.get("op_type", ""))
    agent_id = str(record.get("agent_id", ""))
    target = str(record.get("target", ""))
    result = str(record.get("result", ""))
    view_result = _safe_result(op, result)
    category = categorize(op)
    outcome = _outcome(op, target, result, category)
    severity = _severity(category, outcome, op, f"{target};{result}")
    attention = severity in {"critical", "warning"}
    return {
        **record,
        "result": view_result,
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "title": _title(op, category, _agent(agent_id), target, result),
        "outcome": outcome,
        "outcome_group": OUTCOME_GROUPS.get(outcome, "ok"),
        "outcome_label": OUTCOME_LABELS.get(outcome, outcome),
        "severity": severity,
        "severity_label": SEVERITY_LABELS[severity],
        "attention": attention,
        "object": _object(op, agent_id, target),
        "group_key": _group_key(str(record.get("ts", ""))),
        "technical": {
            "seq": record.get("seq"),
            "op_type": op,
            "target": target,
            "result": view_result,
        },
    }


def _matches(record: Mapping[str, Any], *, category: str, outcome: str,
             outcome_group: str, attention: bool | None) -> bool:
    if category and record["category"] != category:
        return False
    if outcome and record["outcome"] != outcome:
        return False
    if outcome_group and record["outcome_group"] != outcome_group:
        return False
    if attention is not None and bool(record["attention"]) is not attention:
        return False
    return True


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_category = {name: 0 for name in PUBLIC_CATEGORY_ORDER}
    by_severity = {name: 0 for name in SEVERITY_LABELS}
    for record in records:
        by_category[record["category"]] += 1
        by_severity[record["severity"]] += 1
    return {
        "total": len(records),
        "attention": sum(1 for record in records if record["attention"]),
        "by_category": by_category,
        "by_severity": by_severity,
    }


def build_audit_view(page: Mapping[str, Any], *, category: str = "",
                     outcome: str = "", outcome_group: str = "",
                     attention: bool | None = None) -> dict[str, Any]:
    """Enrich one raw audit page without changing its pagination contract.

    The optional filters are applied to the page that was read, so callers
    that need filtered pagination must use :func:`query_audit_view`.
    """
    raw_records = page.get("records", [])
    records = [build_view_record(record) for record in raw_records
               if isinstance(record, Mapping) and _visible(record)]
    records = [record for record in records
               if _matches(record, category=category, outcome=outcome,
                           outcome_group=outcome_group,
                           attention=attention)]
    return {
        **page,
        "records": records,
        "summary": _summary(records),
    }


def _as_attention(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _bounded_int(value: object, name: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"invalid audit {name}")
    return value


def query_audit_view(fetch_page: Callable[[dict[str, Any]], Mapping[str, Any]],
                     query: Mapping[str, Any], *, chunk: int = 200,
                     max_scan: int = 5000) -> dict[str, Any]:
    """Read filtered pages while keeping the raw snapshot/cursor contract.

    securityd can only filter by ``all``/``hook``/``deny``.  Product categories
    and outcomes are derived here, so a filtered page may span several raw
    pages.  Scanning is bounded per request; when the bound is hit, the next
    cursor is the last row that was actually examined, which guarantees
    progress without dropping matching rows.
    """
    limit = _bounded_int(query.get("limit"), "limit", 100, 200)
    cursor = query.get("cursor") or 0
    if type(cursor) is not int or not 0 <= cursor <= 2**63 - 1:
        raise ValueError("invalid audit cursor")
    snapshot = query.get("snapshot_seq")
    if snapshot is not None and (type(snapshot) is not int or not 0 <= snapshot <= 2**63 - 1):
        raise ValueError("invalid audit snapshot_seq")
    category = str(query.get("category") or "")
    if category and category not in PUBLIC_CATEGORY_ORDER:
        raise ValueError("invalid audit category")
    outcome = str(query.get("outcome") or "")
    outcome_group = str(query.get("outcome_group") or "")
    if outcome_group and outcome_group not in set(OUTCOME_GROUPS.values()):
        raise ValueError("invalid audit outcome_group")
    attention = _as_attention(query.get("attention"))
    raw_query = {key: query[key] for key in ("filter", "from", "to", "agent_id")
                 if key in query}
    raw_query["limit"] = chunk

    records: list[dict[str, Any]] = []
    scanned = 0
    scanned_cursor = cursor
    has_more = False
    next_cursor = None
    while True:
        request = {**raw_query, "cursor": cursor}
        if snapshot is not None:
            request["snapshot_seq"] = snapshot
        page = fetch_page(request)
        snapshot = page["snapshot_seq"]
        raw_records = [record for record in page.get("records", [])
                       if isinstance(record, Mapping)]
        scanned += len(raw_records)
        for raw in raw_records:
            if not _visible(raw):
                continue
            view = build_view_record(raw)
            if _matches(view, category=category, outcome=outcome,
                        outcome_group=outcome_group,
                        attention=attention):
                records.append(view)
        if raw_records:
            scanned_cursor = int(raw_records[-1]["seq"])
        if len(records) > limit:
            records = records[:limit]
            has_more = True
            next_cursor = int(records[-1]["seq"])
            break
        if not page.get("has_more") or not raw_records:
            break
        if scanned >= max_scan:
            has_more = True
            next_cursor = int(records[-1]["seq"]) if records else scanned_cursor
            break
        following = int(page["next_cursor"])
        if cursor and following >= cursor:
            # A page that does not move backwards would loop forever.  Keep
            # the rows already examined and stop instead of spinning.
            has_more = True
            next_cursor = int(records[-1]["seq"]) if records else scanned_cursor
            break
        cursor = following

    summary = _summary(records)
    summary["scanned"] = scanned
    result: dict[str, Any] = {"records": records, "snapshot_seq": snapshot,
                              "has_more": has_more, "next_cursor": next_cursor,
                              "summary": summary}
    result["filters"] = {"category": category, "outcome": outcome,
                         "outcome_group": outcome_group,
                         "attention": attention}
    return result
