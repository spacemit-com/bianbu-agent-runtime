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


async def after_run(run: dict) -> dict | None:
    SecurityDenied, get_bridge, resolve_agent_id = _bootstrap()
    bridge = get_bridge()
    if not bridge.available:
        return None
    agent_id = resolve_agent_id(run["agent_id"], client_provider=bridge.client)
    text = str(run["metadata"].get("output") or run["metadata"].get("response") or "")

    payload = {"stage": "output"}
    if text:
        try:
            result = await bridge.invoke(
                bridge.hook_point("output"), agent_id, text,
                session_id=run["session_id"])
            payload["decision"] = result.decision.value
            meta = dict(result.metadata or {})
            meta.pop("_handler_trace", None)
            payload.update(meta)
            if result.modified_data is not None:
                payload["dlp_final_output"] = result.modified_data
        except Exception:  # noqa: BLE001
            pass

    # session 结束 → 清零 PII 映射表（含明文，只在内存）
    try:
        shutdown = await bridge.invoke(
            bridge.hook_point("agent_shutdown"), agent_id,
            {"exit_reason": "run_complete"}, session_id=run["session_id"])
        payload["pii_cleared"] = (shutdown.metadata or {}).get(
            "pii_mappings_cleared", 0)
    except Exception:  # noqa: BLE001
        pass

    return dict(metadata={"securityd": payload})


if __name__ == "__main__":
    from protocol import main

    main(after_run)
