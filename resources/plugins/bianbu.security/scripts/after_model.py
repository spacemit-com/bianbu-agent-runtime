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


async def after_model(run: dict) -> dict | None:
    SecurityDenied, get_bridge, resolve_agent_id = _bootstrap()
    bridge = get_bridge()
    if not bridge.available:
        return None
    text = str(run["metadata"].get("response") or run["metadata"].get("output") or "")
    if not text:
        return None
    agent_id = resolve_agent_id(run["agent_id"], client_provider=bridge.client)
    try:
        result = await bridge.invoke(
            bridge.hook_point("post_model_call"), agent_id, text,
            session_id=run["session_id"])
    except Exception:  # noqa: BLE001
        return None

    payload = {"stage": "post_model_call", "decision": result.decision.value}
    meta = dict(result.metadata or {})
    meta.pop("_handler_trace", None)
    payload.update(meta)
    if result.decision.value == "BLOCK":
        bridge.audit(agent_id, "hook:post_model_call", "BLOCK",
                     str(result.message)[:200])
        raise SecurityDenied(
            tool="<model>", capability="model.output",
            reason=result.message or "模型输出含受限敏感信息",
        )
    if result.decision.value == "MODIFY" and result.modified_data is not None:
        payload["dlp_masked_output"] = result.modified_data
    return dict(
        metadata={"securityd": payload},
        # Read after ``invoke``: masking a tool result or model output creates
        # stand-ins that did not exist when the Hook started.
        stream_stand_ins=bridge.session_state(run["session_id"]).get("pii_map") or None,
    )


def _warmup():
    _bootstrap()
    from lib import warm_bridge

    warm_bridge()


if __name__ == "__main__":
    from protocol import main

    main(after_model, warmup=_warmup)
