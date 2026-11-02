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
    denial = None
    user_visible_text = None
    if text:
        try:
            result = await bridge.invoke(
                bridge.hook_point("output"), agent_id, text,
                session_id=run["session_id"])
            payload["decision"] = result.decision.value
            meta = dict(result.metadata or {})
            meta.pop("_handler_trace", None)
            payload.update(meta)
            if result.decision.value == "BLOCK":
                denial = SecurityDenied(
                    tool="<run>", capability="output.scan",
                    reason=result.message or "最终输出含受限敏感信息",
                )
            elif result.modified_data is not None:
                payload["dlp_final_output"] = result.modified_data
            # The user is entitled to see their own values.  securityd has just
            # restored this session's stand-ins and re-scanned the result, so
            # hand that governed text to the Runtime as the user-visible body.
            # Raw stream deltas may still contain stand-ins; the terminal value
            # is what the console renders once the Run finishes.
            governed = (
                result.modified_data
                if result.modified_data is not None else text
            )
            if governed != text:
                user_visible_text = str(governed)
        except PermissionError as exc:
            denial = exc
        except Exception:  # noqa: BLE001
            pass

    # A Run ends on every user turn, but the Session's stand-in map must
    # survive so the next turn can still restore an earlier stand-in.  Only an
    # explicit Session termination clears it; report the retained state here.
    try:
        shutdown = await bridge.invoke(
            bridge.hook_point("agent_shutdown"), agent_id,
            {"exit_reason": "run_complete"}, session_id=run["session_id"])
        payload["pii_mappings_retained"] = bool(
            (shutdown.metadata or {}).get("pii_mappings_retained", True))
    except Exception:  # noqa: BLE001
        pass

    # The shutdown probe still runs when the output is vetoed so a future
    # explicit Session close has an up-to-date view of the map.
    if denial is not None:
        raise denial
    response = dict(metadata={"securityd": payload})
    if user_visible_text is not None:
        response["user_visible_text"] = user_visible_text
    return response


def _warmup():
    _bootstrap()
    from lib import warm_bridge

    warm_bridge()


if __name__ == "__main__":
    from protocol import main

    main(after_run, warmup=_warmup)
