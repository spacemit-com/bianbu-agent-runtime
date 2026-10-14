from __future__ import annotations

from pathlib import Path

import asyncio
from dataclasses import dataclass
import logging
import os
import re
import uuid
from typing import Mapping

import httpx



logger = logging.getLogger(__name__)
_PRIVATE_METADATA_KEYS = frozenset(
    {"request", "text", "raw", "user_input", "password", "api_key", "token"}
)

_TERMINAL_QUERY_GUIDANCE = """## 终端查询的执行位置
仅对实际操作请求使用以下计划；解释性问题正常回答，不强迫用户选择执行方式。
用户可能要求两种不同的计划：
A. 打开终端，在该终端中执行查询命令，并在终端保留、显示结果。
B. 打开终端，另外调用只读查询工具，在当前应用/聊天中显示结果。
结合原话和当前会话历史判断；仅有“进行”“然后”不能确定展示位置。
用户明确指定“在终端里执行/显示”或“把结果显示在这里”时，采用相应计划，不重复询问。
如果执行位置仍有歧义，先问：“你希望在终端里执行并显示 IP，还是打开终端后，把 IP 显示在这里？”
等待回答前不要打开终端或执行查询；这不是缺少 app_name，不要求用户重复应用名。
“终端里”“这里”等短答只在当前会话确有上述待选任务时补全计划；否则询问它指的是哪个任务，不猜测历史。
保留用户的否定、条件、取消及顺序要求；取消后不要执行旧任务。
选择 A 时只使用实际可用且能在同一终端中执行的能力；后台运行查询不等于在终端执行。
选择 B 时分别验证终端启动与只读查询结果，不把后台结果说成终端中的输出。
复用本轮及历史已完成步骤，避免重复打开窗口；部分失败须明确说明。
缺少所需工具时说明能力限制并征求替代方案，不虚构工具、不擅自将 A 改成 B，不把发出命令说成验证成功。
"""
_TERMINAL_CONTEXT_REPLY = re.compile(
    r"^(?:在)?(?:终端里|终端中|这里|聊天里|聊天中|应用里|应用中)"
    r"(?:显示|执行|显示结果)?[。！!]*$"
)
_TERMINAL_EXPLANATION = re.compile(
    r"解释|介绍|原理|教程|教学|是什么|如何|怎么"
    r"|\b(?:explain|tutorial|how\s+to|what\s+is)\b", re.I,
)


def _terminal_query_topic(request: str) -> bool:
    text = request.casefold()
    return (
        any(term in text for term in ("终端", "命令行", "qterminal", "terminal"))
        and re.search(r"(?<![a-z])ip(?![a-z])", text) is not None
        and any(term in text for term in ("查", "查看", "显示", "ip addr", "query", "check", "show"))
    )


def _terminal_query_context(request: str) -> bool:
    return _terminal_query_topic(request) and not _TERMINAL_EXPLANATION.search(request)


_SIDE_EFFECT_GUARD = """## 设备操作结果的真实性约束（必须遵守）
用户要求打开/关闭/启动/退出/安装/卸载应用、软件时，只有在本轮真正调用了对应工具并拿到
成功结果后，才能对用户声称"已打开 / 已关闭 / 已安装 / 已启动"。没有调用工具时不得声称
执行成功，也不得复述、补全或推测执行结果；工具报未找到、失败或需确认时，如实说明并给出
下一步，不要包装成成功。多目标请求（如"打开终端和浏览器"）必须对每个目标分别调用工具，
漏掉的即未执行，须明确指出。优先使用 bibit-intent 提供的 launch_app / launch_apps /
close_app / close_apps / open_file / pkg_install 等端侧工具完成设备操作。
打开本地文件或文档（尤其是 file_search 返回路径后）必须使用 open_file(path)；
不要用 terminal 直接运行 libreoffice/xdg-open，也不要为了打开文档调用 launch_app。"""
_SIDE_EFFECT_INTENT = re.compile(
    r"打开|启动|运行|开启|关闭|退出|停止|结束|安装|卸载|重装|装一下"
    r"|\b(?:open|launch|start|close|quit|install|uninstall)\b",
    re.I,
)


def _side_effect_sections(request: str) -> tuple[str, ...]:
    """云端兜底前注入真实性约束，避免模型不调用工具却复述执行成功。"""
    if not _SIDE_EFFECT_INTENT.search(request or ""):
        return ()
    return (_SIDE_EFFECT_GUARD,)


def _cloud_patch(request: str, metadata: dict[str, object], *, unconfirmed: bool = False) -> dict:
    """交还 Runtime 模型时的上下文：只带通用约束，不做路由判断。"""
    guidance = _terminal_query_context(request) or _TERMINAL_CONTEXT_REPLY.fullmatch(request.strip())
    sections: list[str] = []
    if unconfirmed:
        sections.append(_UNCONFIRMED_EDGE_CONTEXT)
    if guidance:
        sections.append(_TERMINAL_QUERY_GUIDANCE)
    sections.extend(_side_effect_sections(request))
    return dict(
        context_sections=tuple(sections),
        metadata=metadata,
    )


_UNCONFIRMED_EDGE_CONTEXT = """## 端侧执行状态未确认
端侧（Bibit Intent）请求已提交，但没有拿到结果：有副作用的操作可能已经部分执行。
不要重复执行（编译、安装、卸载、写文件、设备控制）；先读取当前状态确认端侧是否已经做过，
再决定是否需要补做。"""


@dataclass(frozen=True, slots=True)
class _ServiceOutcome:
    handled: bool
    reply: str
    route: str
    reason: str
    metadata: Mapping[str, object]


class _ServiceClient:
    """Runtime-owned client for Bibit's stable local HTTP boundary."""

    def __init__(self, *, base_url: str, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def run(self, prompt: str, session_id: str, run_id: str) -> _ServiceOutcome:
        # Connecting to the local service must not consume its execution budget.
        # A submitted request keeps the full read timeout before the Runtime
        # model takes over; the Hook never decides routing from the error type.
        timeout = httpx.Timeout(self._timeout, connect=min(5.0, self._timeout))
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                f"{self._base_url}/v1/runs",
                json={
                    "run_id": run_id,
                    "session_id": session_id,
                    "prompt": prompt,
                    "timeout_ms": 90000,
                    "stream": False,
                },
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Bibit service response must be a JSON object")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Bibit service metadata must be a JSON object")
        if not isinstance(payload.get("handled"), bool):
            raise ValueError("Bibit service handled must be a boolean")
        return _ServiceOutcome(
            handled=bool(payload.get("handled", False)),
            reply=str(payload.get("reply", "")),
            route=str(payload.get("route", "")),
            reason=str(payload.get("reason", "")),
            metadata=metadata,
        )

    async def cancel(self, run_id: str) -> None:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            response = await client.delete(f"{self._base_url}/v1/runs/{run_id}")
            if response.status_code != 404:
                response.raise_for_status()

def _metadata(outcome) -> dict[str, object]:
    metadata = {
        str(key): value
        for key, value in dict(outcome.metadata).items()
        if str(key).casefold() not in _PRIVATE_METADATA_KEYS
    }
    metadata["route"] = str(outcome.route)
    if outcome.reason:
        metadata["reason"] = str(outcome.reason)
    return metadata


async def before_run(run: dict) -> dict | None:
    service_client = _ServiceClient(
        base_url=os.environ.get("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:8765"),
        timeout=110.0,
    )
    if _TERMINAL_CONTEXT_REPLY.fullmatch(run["request"].strip()):
        return _cloud_patch(run["request"], {"route": "cloud", "reason": "contextual_reply"})
    if _terminal_query_topic(run["request"]):
        if _TERMINAL_EXPLANATION.search(run["request"]):
            return dict(metadata={"route": "cloud", "reason": "open_question"})
        return _cloud_patch(run["request"], {"route": "cloud", "reason": "multi_intent"})
    run_id = uuid.uuid4().hex
    try:
        outcome = await service_client.run(run["request"], run["session_id"], run_id)
    except (Exception, asyncio.CancelledError) as exc:
        # 端侧服务不可用时一律交给云端；已提交的请求先尽力停掉，避免重复执行。
        error_type = type(exc).__name__
        submitted = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        logger.warning(
            "Bibit service unavailable (%s); falling back to the Runtime model",
            error_type,
        )
        if submitted:
            try:
                await asyncio.wait_for(service_client.cancel(run_id), timeout=3.0)
            except (Exception, asyncio.CancelledError):
                logger.warning("Bibit run cancellation could not be confirmed")
        return _cloud_patch(
            run["request"],
            {
                "route": "cloud",
                "reason": "edge_error",
                "edge_error_type": error_type,
            },
            unconfirmed=submitted,
        )

    metadata = _metadata(outcome)
    reply = str(outcome.reply).strip()
    if outcome.handled:
        if not reply:
            reply = "端侧请求已处理。"
            metadata["reason"] = "empty_edge_reply"
        return dict(direct_reply=reply, metadata=metadata)
    return _cloud_patch(run["request"], metadata)


if __name__ == "__main__":
    from protocol import main

    main(before_run)
