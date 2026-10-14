"""appcli 产物独立复核 Hook（只读，绝不再执行渲染）。

模型调用 `appcli_run` 之后，这里**独立**问一次 Bibit Intent 的 `appcli_verify`
（另一个进程、重新读盘），确认这次调用声称的产物真的存在、格式完整。复核不通过时把
一句纠正作为 `context_sections` 附到工具结果里（Runtime 会拼成 "Hook context:"），
模型因此不会凭「命令 exit 0」或「我记得生成了」就宣布完成。

为什么要独立复核而不是相信工具自己的返回：`appcli_run` 已经按产物判过成功，但那
是**同一次调用内**的判定；渲染类 appcli 有「exit 0 但什么都没产出」的真实反例，也有
超时后留下半成品的情况。Hook 换一条路径、重新读文件，才能发现「工具/模型都说成功、
盘上没有合格文件」。

边界（对应 docs/design/cli_anything_integration.md 的 H13）：
- 只调用只读工具 `appcli_verify`，不执行渲染、不安装、不改任何文件；
- 拿不到产物参数（模型用别的参数名，或本来就没有产物）就什么也不做；
- 任何异常都吞掉并回空补丁：复核失败绝不能影响工具调用本身；
- `BIBIT_INTENT_APPCLI_CHECK=0` 可整体关闭。
"""
from __future__ import annotations

import os
import re
import time
import uuid

import httpx

#: executor 派发工具前要求「剩余预算 ≥ 40s」，比这更小会被判 edge_deadline_exceeded
#: 而根本不派发；客户端实际等多久由 BIBIT_INTENT_APPCLI_TIMEOUT 决定（尽力而为）。
_EDGE_TIMEOUT_MS = 60_000
_STATE_KEY = "appcli_artifact"
_VERIFY_PREFIX = "校验产出物"
#: appcli_verify 的结论行；`未产出`/`路径错误` 都算「没有可交付的产物」。
_PASS_MARKERS = ("校验通过",)
_FAIL_MARKERS = ("校验未通过", "未产出", "路径错误", "无法校验")
_GUIDANCE = (
    "这是独立复核的结果：它来自另一次只读校验，不代表本轮要重跑渲染。"
    "如果产物不合格，不要告诉用户任务已完成；说明缺口并让用户决定下一步"
    "（改参数、换动作或延长超时），不要自行重放渲染。"
)
#: 产物参数按登记表约定叫 output；模型也可能把它写进 args 列表。
_ARG_OUTPUT_RE = re.compile(r"^\s*output\s*=\s*(?P<path>.+?)\s*$")


def _artifact_target(tool_input: dict) -> tuple[str, str, str] | None:
    """从工具入参里取 (appcli, action, 产物路径)；取不到就返回 None。

    只认登记表约定的产物参数名 `output`（也接受它出现在 `args` 的 `key=value` 里）。
    路径是模型给的原样值：相对路径由服务端按 appcli 工作区解析，这里不做猜测。
    """
    appcli = str(tool_input.get("appcli") or "").strip()
    action = str(tool_input.get("action") or "").strip()
    if not appcli or not action:
        return None
    raw = str(tool_input.get("output") or "").strip()
    if not raw:
        args = tool_input.get("args")
        if isinstance(args, str):
            args = [item for item in re.split(r"[,，、;；\s]+", args) if item]
        if isinstance(args, (list, tuple)):
            for item in args:
                match = _ARG_OUTPUT_RE.match(str(item))
                if match:
                    raw = match.group("path").strip()
                    break
    if not raw:
        return None
    return appcli, action, raw


def _verdict(reply: str) -> str:
    """判定复核结论：pass / fail / 空（服务端没给可用结论）。

    结论行取自返回的第一行（`校验通过：<路径>` / `校验未通过：<路径>` / `未产出：…`），
    后面的逐条证据只在失败时作为补充信息带上一行，避免把整段 JSON 灌进上下文。
    """
    for line in str(reply or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if any(marker in line for marker in _PASS_MARKERS):
            return "pass"
        if any(marker in line for marker in _FAIL_MARKERS):
            return "fail"
        return ""  # 第一行既不是通过也不是失败：没有可用结论
    return ""


async def _verify(appcli: str, action: str, target: str) -> str:
    base_url = os.environ.get("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
    timeout = float(os.environ.get("BIBIT_INTENT_APPCLI_TIMEOUT", "20"))
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(
            f"{base_url}/v1/runs",
            json={
                "run_id": uuid.uuid4().hex,
                "session_id": "bibit-appcli-verify",
                "prompt": f"{_VERIFY_PREFIX} {appcli} {action} {target}",
                "timeout_ms": _EDGE_TIMEOUT_MS,
                "stream": False,
            },
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("handled"):
        return ""
    return str(payload.get("reply") or "")


def _evidence(reply: str, limit: int = 3) -> str:
    """失败时带上最多几条逐条证据（缩进行），供模型解释到底哪里不合格。"""
    lines = [line.strip() for line in str(reply or "").splitlines() if line.strip()]
    detail = [line for line in lines[1:] if line.startswith(("exists", "size>0", "pdf_", "png_", "jpeg_", "mp4_", "odf_", "office_", "svg_", "xml_"))]
    return "\n".join(f"  - {line}" for line in detail[:limit])


async def after_appcli(run: dict) -> dict | None:
    if os.environ.get("BIBIT_INTENT_APPCLI_CHECK", "1") == "0":
        return None
    target = _artifact_target(dict(run.get("tool_input") or {}))
    if target is None:
        return None
    appcli, action, artifact = target

    state = dict(run.get("state") or {})
    now = time.time()
    try:
        reply = await _verify(appcli, action, artifact)
    except Exception:  # noqa: BLE001 - 复核失败不能影响工具调用
        state[_STATE_KEY] = {"appcli": appcli, "action": action, "status": "unavailable", "at": now}
        return {"state": state}

    verdict = _verdict(reply)
    state[_STATE_KEY] = {
        "appcli": appcli, "action": action, "artifact": artifact,
        "status": verdict or "unknown", "at": now,
    }
    if verdict != "fail":
        return {"state": state}

    section = (
        f"独立复核未通过：{appcli}/{action} 的产物 {artifact} 目前不合格。\n"
        f"{_GUIDANCE}"
    )
    evidence = _evidence(reply)
    if evidence:
        section += "\n复核证据：\n" + evidence
    return {
        "context_sections": [section],
        "state": state,
        "metadata": {_STATE_KEY: {"artifact": artifact, "status": "fail"}},
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent))
    from protocol import main

    main(after_appcli)
