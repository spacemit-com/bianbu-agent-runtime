"""INT-RT-02 — 工具调用判权（判权主战场）。

经 securityd 干预点框架 PRE_TOOL_CALL：
  sec_capability@10  CheckPermission → ALLOW / CONFIRM / BLOCK
  dlp_pre_tool@50    扫描参数 PII，可把 ALLOW 提升为 CONFIRM
  audit@90           写 SHA-256 哈希链

拦截靠 raise SecurityDenied（继承 PermissionError）—— bianbu_agent 的
plugins/host.py 对 BEFORE_TOOL 专门重抛 PermissionError，是项目既有惯例。

CONFIRM 不在此处阻塞：hook 是同步返回、无法触发 LangGraph 的 interrupt，
故写入 dict.metadata 交由 B 类中间件的 HITL 通道处理。

保留兼容：原占位实现读 security_denied / requires_approval 预设标记，
这两条仍生效（供测试与手工兜底），在 securityd 判权之前先判。
"""
from __future__ import annotations

import os

import sys
from collections.abc import Mapping
from pathlib import Path


def _bootstrap():
    """把插件根目录加入 sys.path 以导入 lib（见 lib/__init__.py 说明）。"""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        # append 而非 insert(0)：插件根下的 lib/ 等目录若与宿主同名，
        # 排在前面会污染宿主的模块解析（见 lib/bridge.py 里的同类说明）
        sys.path.append(str(root))
    from lib import (  # noqa: PLC0415
        UNMAPPED_CAPABILITY,
        SecurityDenied,
        egress_info,
        get_bridge,
        resolve_agent_id,
        resolve_capability,
        unmapped_decision,
        unmapped_policy,
    )
    return (UNMAPPED_CAPABILITY, SecurityDenied, get_bridge,
            resolve_agent_id, resolve_capability,
            unmapped_decision, unmapped_policy, egress_info)


async def before_tool(run: dict) -> dict | None:
    (UNMAPPED_CAPABILITY, SecurityDenied, get_bridge,
     resolve_agent_id, resolve_capability,
     unmapped_decision, unmapped_policy, egress_info) = _bootstrap()
    # —— 兼容原占位实现的预设标记 ——
    bridge = get_bridge()
    provider = bridge.client if bridge.available else None
    agent_id = resolve_agent_id(run["agent_id"], client_provider=provider)
    from lib import agent_security_mode_is_yolo
    yolo = agent_security_mode_is_yolo(agent_id, client_provider=provider)
    if run["metadata"].get("security_denied") is True and not yolo:
        raise SecurityDenied(
            tool=str(run["metadata"].get("tool") or run["request"]),
            capability="<preset>",
            reason="tool call denied by bianbu.security")
    if (run["metadata"].get("requires_approval") is True and not yolo
            and run["metadata"].get("approval_granted") is not True):
        raise SecurityDenied(
            tool=str(run["metadata"].get("tool") or run["request"]),
            capability="<preset>",
            reason="tool call requires approval")

    tool = str(run["metadata"].get("tool") or run["request"] or "")
    args = run["metadata"].get("arguments") or {}
    tool_metadata = run["metadata"].get("tool_metadata") or {}
    if not tool:
        return None

    resolved = resolve_capability(
        tool, args, client_provider=provider,
        tool_metadata=(dict(tool_metadata) if isinstance(tool_metadata, Mapping) else {}),
    )

    if resolved is None:
        # 未映射工具（D-5）：原来 DEFAULT_ALLOW=True 就直接放行 —— 不调
        # securityd、不写审计，等于"名字不含关键字的新工具全部免判权"，
        # 而运维在配置台上看不到任何痕迹。现按统一策略处置。
        policy = unmapped_policy()
        code = unmapped_decision()
        bridge.audit(resolve_agent_id(run["agent_id"], client_provider=provider),
                     tool, f"{UNMAPPED_CAPABILITY}:", f"unmapped:{policy}")
        if code == 2 and not yolo:
            raise SecurityDenied(
                tool=tool, capability=UNMAPPED_CAPABILITY,
                reason=f"工具 {tool} 未在安全能力映射表中声明")
        payload = {"decision": "ALLOW" if (code == 0 or yolo) else "CONFIRM",
                   "capability": UNMAPPED_CAPABILITY,
                   "scope": tool,
                   "reason": "unmapped_tool", "tool": tool,
                   "unmapped_policy": policy}
        if code == 1 and not yolo:
            # 与下方 CONFIRM 分支同一机制：hook 无法阻塞，标记交
            # HITL 通道弹审批。真正的审批发生在中间件的
            # securityd_requires_confirm（after_model 阶段，早于本 hook）。
            payload["requires_approval"] = True
        return dict(metadata={"securityd": payload})

    capability, scope = resolved
    if not capability:
        return None  # 显式免判权（如 clarify）

    agent_id = resolve_agent_id(run["agent_id"], client_provider=provider)

    if not bridge.available:
        bridge.degrade(tool, capability, bridge.reason)  # 高危抛，低危放行
        return dict(metadata={
            "securityd": {"decision": "ALLOW", "degraded": True,
                          "capability": capability, "tool": tool}})

    try:
        # D-9：远端 MCP 的参数会发给第三方服务器，DLP 要据此提一档
        # （运维把"姓名"配成 allow 指的是本地工具用它无所谓，
        # 发到外部 server 是另一回事，且数据出境不可撤回）。
        payload_in = {"tool": tool, "capability": capability,
                      "scope": scope, "args": dict(args)}
        egress = egress_info(
            tool,
            dict(tool_metadata) if isinstance(tool_metadata, Mapping) else {},
        )
        if egress is not None:
            payload_in["egress"] = egress
        result = await bridge.invoke(
            bridge.hook_point("pre_tool_call"), agent_id, payload_in,
            session_id=run["session_id"],
            run_id=run["run_id"])
    except Exception as e:  # noqa: BLE001 — 总线/框架异常走 fail 分档
        bridge.degrade(tool, capability, e)
        return dict(metadata={
            "securityd": {"decision": "ALLOW", "degraded": True,
                          "capability": capability, "tool": tool,
                          "error": str(e)}})

    decision = result.decision.value
    meta = dict(result.metadata or {})
    meta.pop("_handler_trace", None)  # trace 已进审计链，不必带进 state
    payload = {
        "decision": decision,
        "capability": capability,
        "scope": scope,
        "agent_id": agent_id,
        "tool": tool,
        **meta,
    }

    if decision == "BLOCK":
        # securityd 的 sec_capability_handler 把"总线不可达"也返回成 BLOCK，
        # 但只在**明确拒绝**时标 reason="denied"。二者必须区分：
        # 明确拒绝 → 直接抛；不可达 → 走 fail 分档（低危放行、高危才抛）。
        # 干预点框架对 handler 异常/超时（reason=handler_error/handler_timeout）
        # 的 fail-closed BLOCK 也是硬拒绝，不能落入"不可达降级"再被低危放行。
        if (meta.get("dlp_action") != "block"
                and meta.get("reason") not in {"denied", "handler_error", "handler_timeout"}):
            bridge.degrade(tool, capability, result.message or "securityd 不可达")
            return dict(metadata={
                "securityd": {"decision": "ALLOW", "degraded": True,
                              "capability": capability, "tool": tool,
                              "error": result.message}})
        # YOLO 能力旁路由 securityd 判权完成；最终 BLOCK 仍是强制约束。
        raise SecurityDenied(
            tool=tool, capability=capability,
            reason=result.message or f"工具 {tool} 被拒绝",
            decision=decision)

    if decision == "CONFIRM":
        # 交 B 类中间件：InterruptOnConfig.when 读此标记决定是否弹审批
        if yolo:
            payload["yolo_override"] = "CONFIRM"
            payload["decision"] = "ALLOW"
        else:
            payload["requires_approval"] = True

    if decision == "MODIFY" and result.modified_data is not None:
        # DLP 脱敏后的参数经 metadata 传给中间件应用（hook 层改不了参数）
        modified = result.modified_data
        payload["dlp_masked_args"] = (
            modified.get("args") if isinstance(modified, dict) else modified)

    return dict(metadata={"securityd": payload})


if __name__ == "__main__":
    from protocol import main

    main(before_tool)
