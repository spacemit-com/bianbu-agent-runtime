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


async def after_context(run: dict) -> dict | None:
    SecurityDenied, get_bridge, resolve_agent_id = _bootstrap()
    bridge = get_bridge()
    if not bridge.available:
        return None
    text = str(run["request"] or "")
    if not text:
        return None
    agent_id = resolve_agent_id(run["agent_id"], client_provider=bridge.client)
    try:
        result = await bridge.invoke(
            bridge.hook_point("input"), agent_id, text,
            session_id=run["session_id"])
    except Exception:  # noqa: BLE001
        return None

    payload = {"stage": "input", "decision": result.decision.value}
    meta = dict(result.metadata or {})
    meta.pop("_handler_trace", None)
    payload.update(meta)
    if result.decision.value == "BLOCK":
        # AFTER_CONTEXT 不在 host.py 的 PermissionError 重抛白名单，
        # 抛了会被吞。改为落标记，由 before_model（在白名单内）拦住。
        payload["deny_run"] = True
        payload["deny_reason"] = result.message
    if result.decision.value == "MODIFY" and result.modified_data is not None:
        payload["dlp_masked_input"] = result.modified_data
    return dict(metadata={"securityd": payload})


if __name__ == "__main__":
    from protocol import main

    main(after_context)
