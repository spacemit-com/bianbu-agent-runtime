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


async def on_error(run: dict) -> dict | None:
    SecurityDenied, get_bridge, resolve_agent_id = _bootstrap()
    bridge = get_bridge()
    if not bridge.available:
        return None
    agent_id = resolve_agent_id(run["agent_id"], client_provider=bridge.client)
    err = str(run["metadata"].get("error") or run["metadata"].get("message") or "unknown")
    tool = str(run["metadata"].get("tool") or "-")
    # 截断：审计字段进哈希链，超长内容会让链记录膨胀
    bridge.audit(agent_id, f"error:{tool}", "on_error", err[:200])
    return None


if __name__ == "__main__":
    from protocol import main

    main(on_error)
