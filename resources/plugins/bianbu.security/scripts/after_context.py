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
        # Every event point may veto the Run; no need to defer to before_model.
        raise SecurityDenied(
            tool="<input>", capability="input.scan",
            reason=result.message or "输入含受限敏感信息",
        )
    if result.decision.value == "CONFIRM":
        payload["requires_approval"] = True
    if result.decision.value == "MODIFY" and result.modified_data is not None:
        payload["dlp_masked_input"] = result.modified_data
    # The private map lets the Runtime restore streamed deltas before they are
    # published.  It is a top-level Hook field, never part of public metadata.
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

    main(after_context, warmup=_warmup)
