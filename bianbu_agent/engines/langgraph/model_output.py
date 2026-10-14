"""Recover an empty provider turn before it can finish the agent graph."""

from __future__ import annotations

import logging
from langgraph.config import get_config, get_stream_writer

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage
from bianbu_agent.engines.reasoning_markup import ThinkStreamParser
from bianbu_agent.runs.recovery import ModelRecoveryExhausted


LOGGER = logging.getLogger(__name__)


def _retry_request(request):
    hint = (
        "The previous model attempt produced no answer or tool calls. "
        "Recover by doing ONLY ONE SMALL concrete step in this response. "
        "Do not plan or implement the entire solution in reasoning. "
        "For a coding task, make one small code change with a tool now "
        "(for a new app, start with a minimal working skeleton), "
        "then implement and test one small part per later tool call. "
        "Preserve existing work. "
        "Do not reread requirements or repeat completed inspection. "
        "Keep the original acceptance requirements; complete them across subsequent steps. "
        "You can continue working after the tool result. "
        "If no tool action is needed, return the user-visible answer now."
    )
    system = request.system_message or SystemMessage(content="")
    content = system.content
    content = (
        content + "\n\n" + hint if isinstance(content, str)
        else [*content, {"type": "text", "text": hint}]
    )
    # Model aliases and reasoning output do not establish support for optional
    # provider parameters. Preserve the caller's settings on the recovery turn.
    return request.override(system_message=system.model_copy(update={"content": content}))


def _empty_output_detail(response) -> str:
    for message in reversed(response.result):
        if not isinstance(message, AIMessage):
            continue
        finish = message.response_metadata.get("finish_reason")
        # Only known enum values are useful diagnostics; never include arbitrary
        # provider text or the actual reasoning in a user-visible error.
        if not isinstance(finish, str) or finish not in {"length", "stop", "tool_calls", "content_filter"}:
            finish = "unknown"
        tokens = (message.usage_metadata or {}).get("output_tokens")
        tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else "unknown"
        reason = "reasoning exhausted output budget; " if (
            finish == "length" and message.additional_kwargs.get("reasoning_content")
        ) else ""
        return f"{reason}finish_reason={finish}, output_tokens={tokens}"
    return "finish_reason=unknown, output_tokens=unknown"


def _has_output(response) -> bool:
    if response.structured_response is not None:
        return True
    try:
        provider = get_config().get("metadata", {}).get("provider_id")
    except RuntimeError:
        provider = None
    for message in response.result:
        if not isinstance(message, AIMessage):
            continue
        if message.tool_calls:
            return True
        content = message.content
        if isinstance(content, str):
            text = content
        else:
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    kind = block.get("type")
                    if kind in {"text", "output_text"}:
                        parts.append(str(block.get("text") or ""))
                    elif kind not in {None, "thinking", "reasoning"}:
                        return True
            text = "".join(parts)
        if provider == "minimax":
            text = "".join(value for kind, value in ThinkStreamParser().feed(text)
                           if kind == "token")
        if text.strip():
            return True
    return False


def _recovery_state() -> dict:
    try:
        control = get_config().get("configurable", {}).get("run_control")
    except RuntimeError:
        control = None
    return control.model_recovery if control is not None else {}


def _announce_recovery(attempt: int) -> None:
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({"kind": "progress", "payload": {
        "phase": "recovering", "attempt": attempt, "max_attempts": 2,
        "message": "模型未返回可执行结果，正在保留已有进度并分步继续。",
    }})


class ModelOutputMiddleware(AgentMiddleware):
    """Keep recovery strategy and a bounded retry budget for the entire Run."""

    def _recover(self, request, response, state):
        attempts = state.get("attempts", 0)
        if attempts >= 2:
            raise ModelRecoveryExhausted("empty_output", attempts,
                "model returned no answer or tool calls; recovery exhausted "
                f"({_empty_output_detail(response)})")
        state["attempts"] = attempts + 1
        LOGGER.warning("Model output recovery %s/2 (%s)", attempts + 1,
                       _empty_output_detail(response))
        _announce_recovery(attempts + 1)
        return _retry_request(request)

    def wrap_model_call(self, request, handler):
        state = _recovery_state()
        current = _retry_request(request) if state.get("attempts") else request
        while True:
            response = handler(current)
            if _has_output(response):
                return response
            current = self._recover(request, response, state)

    async def awrap_model_call(self, request, handler):
        state = _recovery_state()
        current = _retry_request(request) if state.get("attempts") else request
        while True:
            response = await handler(current)
            if _has_output(response):
                return response
            current = self._recover(request, response, state)
