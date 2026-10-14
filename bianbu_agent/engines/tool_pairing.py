"""Keep provider tool calls and tool results paired on the wire.

OpenAI-compatible providers reject a request as ``invalid params`` when an
assistant message declares a tool call that the following ``tool`` messages do
not answer. LangChain drops tool calls whose arguments are not a JSON object
(``MiniMax`` returns ``["all"]`` for a positional call) into
``AIMessage.invalid_tool_calls`` while still serializing them as tool calls for
the next request, so a single malformed call used to make every later Run in
that Session fail with HTTP 400.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from typing import Any

LOGGER = logging.getLogger(__name__)

INVALID_ARGUMENTS_RESULT = (
    "This tool call was not executed because the model returned arguments that "
    "are not a JSON object. Call the tool again with an object that matches its "
    "argument schema."
)
UNFINISHED_TOOL_RESULT = (
    "This tool call was not executed because the Run ended before the tool "
    "produced a result."
)


def _call_entries(message: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return ``(call_id, name)`` for the tool calls declared by one message."""

    raw = message.get("tool_calls")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    entries: list[tuple[str, str]] = []
    for call in raw:
        if not isinstance(call, Mapping):
            continue
        call_id = str(call.get("id") or "")
        if not call_id:
            # A call without an id cannot be paired with a result message.
            continue
        function = call.get("function")
        name = ""
        if isinstance(function, Mapping):
            name = str(function.get("name") or "")
        entries.append((call_id, name))
    return entries


def _tool_result(call_id: str, name: str, content: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
    }
    if name:
        result["name"] = name
    return result


def reconcile_tool_pairs(
    messages: Sequence[Mapping[str, Any]],
    *,
    invalid_call_ids: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Return provider messages where tool calls and tool results match.

    Answers every unanswered tool call and drops every orphan tool result.
    ``invalid_call_ids`` names the calls LangChain classified as invalid so the
    synthetic result can explain the real reason instead of a generic one.
    """

    invalid = {str(value) for value in invalid_call_ids if str(value or "")}
    paired: list[dict[str, Any]] = []
    unanswered: dict[str, str] = {}
    injected: list[str] = []
    dropped_orphans: list[str] = []

    def flush() -> None:
        for call_id, name in unanswered.items():
            reason = (
                INVALID_ARGUMENTS_RESULT if call_id in invalid
                else UNFINISHED_TOOL_RESULT
            )
            paired.append(_tool_result(call_id, name, reason))
            injected.append(call_id)
        unanswered.clear()

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "")
        if role == "assistant":
            flush()
            paired.append(dict(message))
            for call_id, call_name in _call_entries(message):
                unanswered[call_id] = call_name
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id and call_id in unanswered:
                del unanswered[call_id]
                paired.append(dict(message))
            else:
                dropped_orphans.append(call_id)
        else:
            flush()
            paired.append(dict(message))
    flush()

    if injected:
        # Surfacing this is the point: it means the provider returned a tool
        # call LangChain could not execute, and masking it silently would hide
        # a provider/tool-schema mismatch from the operator.
        LOGGER.warning(
            "provider 请求补齐了 %d 条未执行的工具结果 call_ids=%s",
            len(injected),
            ",".join(injected),
        )
    if dropped_orphans:
        LOGGER.warning(
            "provider 请求丢弃了 %d 条无对应调用的工具结果 call_ids=%s",
            len(dropped_orphans),
            ",".join(dropped_orphans),
        )
    return paired


__all__ = ["reconcile_tool_pairs", "INVALID_ARGUMENTS_RESULT", "UNFINISHED_TOOL_RESULT"]
