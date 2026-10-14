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


async def before_run(run: dict) -> dict | None:
    SecurityDenied, get_bridge, resolve_agent_id = _bootstrap()
    bridge = get_bridge()
    if not bridge.available:
        return None
    agent_id = resolve_agent_id(run["agent_id"], client_provider=bridge.client)
    try:
        result = await bridge.invoke(
            bridge.hook_point("agent_startup"), agent_id,
            {"manifest": {}}, session_id=run["session_id"])
    except Exception:  # noqa: BLE001 — 策略加载失败不阻断 run 启动
        return None
    if result.decision.value == "BLOCK":
        raise SecurityDenied(
            tool="<run>", capability="agent.start",
            reason=result.message or "run 启动被拒绝")
    return dict(metadata={"securityd": {
        "stage": "before_run", "agent_id": agent_id,
        "decision": result.decision.value}})


if __name__ == "__main__":
    from protocol import main

    main(before_run)
