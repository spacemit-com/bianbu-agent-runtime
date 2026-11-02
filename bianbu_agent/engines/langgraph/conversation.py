"""Rebuild provider-facing messages from a Session transcript.

Without a checkpointer the graph is stateless: every Run hands LangGraph the
whole conversation it should continue.  The Session transcript is the only
authority for that history, so this module folds it back into the message list
the model saw.

Two rules make the fold trustworthy:

* The fold is *lossless for the request*, not a summary.  Text deltas are
  concatenated, tool calls keep their original call ids, and tool results keep
  their original content, so the rebuilt request is the one the provider
  already accepted.
* Events that only describe runtime bookkeeping (progress, hooks, usage,
  compression markers, inbox claims) never become messages.  Compaction is a
  derived optimisation, not a fact: refolding the full history and letting the
  compression middleware compact it again produces the same request, so the
  fold does not replay ``context/compressed``.

A Run that died between a tool call and its result leaves that call unanswered
in the log.  The fold closes it with a synthetic error result, which is what
keeps the next provider request valid -- the same repair the checkpoint-based
implementation performed on saved state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

from bianbu_agent.engines.names import encode_tool_name
from bianbu_agent.sessions.events import SessionEvent

#: Events that carry no model-visible message content.
_IGNORED_TYPES = frozenset(
    {
        "agent/inbox/claimed",
        "agent/inbox/spliced",
        "agent/input.applied",
        "context/compressed",
        "hook/invoked",
        "hook/result",
        "request/context",
        "run/cancelled",
        "run/completed",
        "run/event",
        "run/failed",
        "run/progress",
    }
)

#: The provider's key for the reasoning that preceded an assistant turn.
_REASONING_KEY = "reasoning_content"

#: The synthetic result a tool call gets when its Run ended before it finished.
_UNFINISHED_TOOL_RESULT = (
    "The Run ended before this tool call completed; the tool was not executed."
)
#: The synthetic result a tool call gets when the Runtime refused to run it.
_UNAVAILABLE_TOOL_RESULT = (
    "The tool was not available to this Agent; the tool was not executed."
)


def fold_messages(
    events: Iterable[SessionEvent],
    *,
    input_id_for_run: Callable[[str], str],
) -> list[BaseMessage]:
    """Fold one Session transcript into the message list to continue from.

    ``input_id_for_run`` must be the same derivation the engine used for the
    HumanMessage it sent, so a replayed user turn carries the id the Run was
    addressed with.
    """

    messages: list[BaseMessage] = []
    open_ai = _OpenAssistant()
    for event in events:
        event_type = str(event.type)
        data = event.data
        if event_type == "user/message":
            open_ai.flush(messages)
            messages.append(_human_message(event, data, input_id_for_run))
        elif event_type == "assistant/text.delta":
            open_ai.add_text(str(data.get("text") or ""))
        elif event_type == "assistant/reasoning.delta":
            open_ai.add_reasoning(str(data.get("text") or ""))
        elif event_type == "assistant/message.end":
            if not data.get("has_tool_calls"):
                open_ai.flush(messages)
        elif event_type == "tool/call.started":
            open_ai.add_tool_call(data)
        elif event_type == "tool/call.rejected":
            open_ai.add_tool_call(data)
            open_ai.flush(messages)
            messages.append(
                _tool_message(data, _UNAVAILABLE_TOOL_RESULT, status="error")
            )
        elif event_type == "tool/call.completed":
            open_ai.flush(messages)
            messages.append(_tool_message(data, str(data.get("result") or "")))
        elif event_type in _IGNORED_TYPES:
            continue
    open_ai.flush(messages)
    return _repair_unanswered_tool_calls(messages)


class _OpenAssistant:
    """The assistant message currently being assembled, not yet in the list."""

    __slots__ = ("_text", "_tool_calls", "_reasoning")

    def __init__(self) -> None:
        self._text: list[str] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._reasoning: list[str] = []

    def add_reasoning(self, text: str) -> None:
        """Accumulate the reasoning that belongs to the turn being assembled.

        Providers that think in a separate channel require an assistant turn to
        carry its reasoning back on any tool-enabled request, so the fold keeps
        it instead of dropping it as bookkeeping.  It is not message content:
        reasoning with no text and no tool call stays out of the history.
        """

        if text:
            self._reasoning.append(text)

    def add_text(self, text: str) -> None:
        if not text:
            return
        if self._tool_calls:
            # Text after tool calls opens the next assistant turn.
            self._text = []
            self._tool_calls = []
            self._reasoning = []
        self._text.append(text)

    def add_tool_call(self, data: Mapping[str, object]) -> None:
        call_id = str(data.get("call_id") or "").strip()
        name = str(data.get("name") or "").strip()
        if not call_id or not name:
            return
        arguments = data.get("arguments")
        self._tool_calls.append(
            {
                # The transcript stores the canonical SDK tool id so the console
                # and audit can show it, but the provider only ever saw the
                # encoded name bound to the tool.  Replaying the raw id made the
                # model copy a name LangChain rejects ("not a valid tool") and
                # OpenAI-compatible endpoints refuse, so encode it again here.
                "name": encode_tool_name(name),
                "args": dict(arguments) if isinstance(arguments, Mapping) else {},
                "id": call_id,
                "type": "tool_call",
            }
        )

    def flush(self, messages: list[BaseMessage]) -> None:
        text = "".join(self._text)
        reasoning = "".join(self._reasoning)
        if not text and not self._tool_calls:
            # Reasoning alone is not a message: a turn that produced no text and
            # no tool call has nothing to continue from.
            self._reasoning = []
            return
        additional = {_REASONING_KEY: reasoning} if reasoning else {}
        messages.append(
            AIMessage(
                content=text,
                tool_calls=list(self._tool_calls),
                additional_kwargs=additional,
            )
        )
        self._text = []
        self._tool_calls = []
        self._reasoning = []


def _human_message(
    event: SessionEvent,
    data: Mapping[str, object],
    input_id_for_run: Callable[[str], str],
) -> HumanMessage:
    message_id = str(data.get("message_id") or "").strip()
    if not message_id:
        message_id = input_id_for_run(str(event.run_id or ""))
    attachments = data.get("attachments")
    additional_kwargs: dict[str, Any] = {}
    if isinstance(attachments, (list, tuple)) and attachments:
        additional_kwargs["attachments"] = [
            dict(value) for value in attachments if isinstance(value, Mapping)
        ]
    return HumanMessage(
        content=str(data.get("content") or ""),
        id=message_id,
        additional_kwargs=additional_kwargs,
    )


def _tool_message(
    data: Mapping[str, object],
    content: str,
    *,
    status: str | None = None,
) -> ToolMessage:
    """One tool result. ``status`` is explicit for results the Runtime made up."""

    selected = status or ("error" if str(data.get("status")) == "error" else "success")
    return ToolMessage(
        content=content,
        tool_call_id=str(data.get("call_id") or ""),
        name=encode_tool_name(str(data.get("name") or "")) if data.get("name") else None,
        status="error" if selected == "error" else "success",
    )


def _repair_unanswered_tool_calls(
    messages: list[BaseMessage],
) -> list[BaseMessage]:
    """Close every tool call that never got a result with an error result."""

    repaired: list[BaseMessage] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        repaired.append(message)
        index += 1
        if not isinstance(message, AIMessage):
            continue
        declared = [
            str(call.get("id") or "")
            for call in message.tool_calls
            if call.get("id")
        ]
        if not declared:
            continue
        answered: set[str] = set()
        while index < len(messages) and isinstance(messages[index], ToolMessage):
            answered.add(str(messages[index].tool_call_id or ""))
            repaired.append(messages[index])
            index += 1
        for call in message.tool_calls:
            call_id = str(call.get("id") or "")
            if not call_id or call_id in answered:
                continue
            repaired.append(
                ToolMessage(
                    content=_UNFINISHED_TOOL_RESULT,
                    tool_call_id=call_id,
                    name=str(call.get("name") or "") or None,
                    status="error",
                )
            )
    return repaired


__all__ = ["fold_messages"]
