from __future__ import annotations

import os

import sys
from pathlib import Path


def _bootstrap():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        # append 而非 insert(0)：插件根下的 lib/ 等目录若与宿主同名，
        # 排在前面会污染宿主的模块解析（见 lib/bridge.py 里的同类说明）
        sys.path.append(str(root))
    from lib import SecurityDenied, get_bridge, resolve_agent_id  # noqa: PLC0415
    return SecurityDenied, get_bridge, resolve_agent_id


async def after_tool(run: dict) -> dict | None:
    SecurityDenied, get_bridge, resolve_agent_id = _bootstrap()
    bridge = get_bridge()
    if not bridge.available:
        return None
    tool = str(run["metadata"].get("tool") or run["request"] or "")
    result_meta = run["metadata"].get("result_metadata") or {}
    content = run["metadata"].get("result") or result_meta.get("content") or ""
    agent_id = resolve_agent_id(run["agent_id"], client_provider=bridge.client)

    if not content:
        # 无返回内容也要记账（审计要能看到工具执行完成）
        bridge.audit(agent_id, tool, "executed", "ok")
        return None

    try:
        result = await bridge.invoke(
            bridge.hook_point("post_tool_call"), agent_id,
            {"tool": tool, "result": content},
            session_id=run["session_id"])
    except Exception:  # noqa: BLE001
        bridge.audit(agent_id, tool, "executed", "ok")
        return None

    payload = {"stage": "post_tool_call", "tool": tool,
               "decision": result.decision.value}
    meta = dict(result.metadata or {})
    meta.pop("_handler_trace", None)
    payload.update(meta)
    if result.decision.value == "MODIFY" and result.modified_data is not None:
        payload["dlp_masked_result"] = result.modified_data
    if result.decision.value == "BLOCK":
        payload["result_blocked"] = True
        payload["block_reason"] = result.message
    return dict(metadata={"securityd": payload})


if __name__ == "__main__":
    from protocol import main

    main(after_tool)
