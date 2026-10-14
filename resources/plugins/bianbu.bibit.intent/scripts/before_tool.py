"""PreToolUse guard for bibit-intent adjustment MCP tools.

The Runtime model can otherwise turn an object-less request such as "调到1"
into a set_volume call even though brightness/backlight are equally plausible.
This hook runs before those MCP tools and denies the call unless the original
request explicitly names the target setting.
"""
from __future__ import annotations

import re

_TARGET_RE = {
    "set_volume": (
        re.compile(r"音量|声音|声量|volume|sound|audio|loudness", re.I),
        "音量/声音",
    ),
    "set_brightness": (
        re.compile(
            r"亮度|背光|屏幕|太暗|太亮|刺眼|看不清|brightness|backlight|dim|brighten|screen",
            re.I,
        ),
        "亮度/背光",
    ),
}


def _user_text(run: dict, tool_name: str) -> str:
    """用户这一轮说的话。

    Runtime 在工具事件里把 ``request`` 放成工具限定名（``.../set_volume``），
    直接拿它做关键词匹配会命中 "volume" 而永远放行；``prompt`` 才是用户输入。
    直接调用 Hook（没有 Run 上下文）时回退到 ``request``。
    """

    prompt = str(run.get("prompt") or "").strip()
    if prompt:
        return prompt
    request = str(run.get("request") or "").strip()
    return "" if request == tool_name else request


async def before_tool(run: dict) -> dict:
    tool_name = str(run.get("tool_name") or "")
    short_name = tool_name.rsplit("/", 1)[-1]
    guard = _TARGET_RE.get(short_name)
    if guard is None:
        return {}

    target_re, label = guard
    if target_re.search(_user_text(run, tool_name)):
        return {}

    return {
        "decision": "deny",
        "reason": (
            f"当前请求没有明确指定要调节{label}，不能调用 {short_name}；"
            "请先询问用户要调节音量还是亮度，再执行对应工具。"
        ),
    }


if __name__ == "__main__":
    from protocol import main

    main(before_tool)
