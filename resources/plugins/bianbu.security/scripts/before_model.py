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
    from lib import SecurityDenied, agent_security_mode_is_yolo, get_bridge, resolve_agent_id  # noqa: PLC0415
    return SecurityDenied, agent_security_mode_is_yolo, get_bridge, resolve_agent_id


async def before_model(run: dict) -> dict | None:
    SecurityDenied, agent_security_mode_is_yolo, get_bridge, resolve_agent_id = _bootstrap()
    bridge = get_bridge()
    provider = bridge.client if bridge.available else None
    agent_id = resolve_agent_id(run["agent_id"], client_provider=provider)
    yolo = agent_security_mode_is_yolo(agent_id, client_provider=provider)
    # 兼容原占位实现
    if run["metadata"].get("security_denied") is True and not yolo:
        raise SecurityDenied(
            tool="<model>", capability="model.invoke",
            reason="run denied by bianbu.security")

    # 兜住 after_context 的 BLOCK（那个点抛异常会被 host 吞掉）
    scoped = run["metadata"].get("bianbu.security", run["metadata"])
    upstream = scoped.get("securityd") if isinstance(scoped, dict) else None
    if not yolo and isinstance(upstream, dict) and upstream.get("deny_run") is True:
        raise SecurityDenied(
            tool="<model>", capability="model.invoke",
            reason=str(upstream.get("deny_reason") or "输入含受限敏感信息"))

    if not bridge.available:
        return None
    prompt = str(run["request"] or "")
    if not prompt:
        return None
    agent_id = resolve_agent_id(run["agent_id"], client_provider=bridge.client)
    endpoint = str(run["metadata"].get("model_endpoint") or "")
    try:
        result = await bridge.invoke(
            bridge.hook_point("pre_model_call"), agent_id,
            {"prompt": prompt, "endpoint": endpoint},
            session_id=run["session_id"])
    except Exception:  # noqa: BLE001
        return None

    if result.decision.value == "BLOCK":
        raise SecurityDenied(
            tool="<model>", capability="model.invoke",
            reason=result.message or "prompt 含受限敏感信息")

    payload = {"stage": "pre_model_call", "decision": result.decision.value}
    meta = dict(result.metadata or {})
    meta.pop("_handler_trace", None)
    payload.update(meta)
    if result.decision.value == "MODIFY" and result.modified_data is not None:
        payload["dlp_masked_prompt"] = result.modified_data
    return dict(metadata={"securityd": payload})


if __name__ == "__main__":
    from protocol import main

    main(before_model)
