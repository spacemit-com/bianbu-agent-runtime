"""Human approval that parks the Run instead of persisting an interrupt.

``HumanInTheLoopMiddleware`` pauses the graph with ``interrupt()``, which only
works against a checkpointer: the decision arrives on a *later* invocation that
resumes from saved state.  Runtime keeps the Run alive instead.  The graph
blocks inside this middleware while the outer Run stream surfaces
``approval_required``, and the user's decision is handed back to the very same
Run.  No graph state is ever written down, so no checkpoint database exists.

The decision semantics are the ones ``HumanInTheLoopMiddleware.after_model``
already implements -- approve, edit, reject or answer each hanging call, then
rewrite the assistant message -- because the user-facing contract must not
change.  Only the source of ``decisions`` differs.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from bianbu_agent.runs.control import RunControl


def _control_from_runtime(runtime: Any) -> RunControl | None:
    """The live Run this graph invocation belongs to, if it is one of ours."""

    config = getattr(runtime, "config", None)
    if not isinstance(config, Mapping):
        # after_model receives LangGraph's Runtime, which has no config
        # attribute; the active invocation's task-local config does, and it is
        # what carries the Run that is waiting for this decision.
        from langgraph.config import get_config

        try:
            config = get_config()
        except RuntimeError:
            config = None
    configurable = (
        config.get("configurable") if isinstance(config, Mapping) else None
    )
    control = (
        configurable.get("run_control")
        if isinstance(configurable, Mapping) else None
    )
    return control if isinstance(control, RunControl) else None


def _field(item: Any, key: str, default: object = None) -> Any:
    """Read one field of an ``ActionRequest``/``ReviewConfig``.

    Those types are TypedDicts at one LangChain version and objects at
    another, so the approval payload must not assume either shape.
    """

    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


class ApprovalMiddleware(HumanInTheLoopMiddleware):
    """Ask the live Run for a decision instead of checkpointing an interrupt."""

    #: Tool name -> tool metadata, reported with the approval request so the
    #: approving caller sees the same contract securityd saw.
    _metadata_by_name: Mapping[str, Mapping[str, object]] = {}

    def after_model(
        self, state: Any, runtime: Any
    ) -> dict[str, Any] | None:
        raise RuntimeError(
            "tool approval requires the async agent path: the live Run has to "
            "await the user's decision, which a synchronous node cannot do"
        )

    def _should_interrupt(
        self,
        tool_call: Any,
        config: Mapping[str, Any],
        state: Any,
        runtime: Any,
    ) -> bool:
        """Decide whether one ``interrupt_on`` entry applies to this call.

        No predicate means the entry always interrupts; otherwise the predicate
        decides, and it reads the tool call off the request.  The request shape
        is the one this package builds for its own predicates, so an entry
        behaves the same no matter which LangChain resolves: ``when`` is a
        Runtime extension the 1.2.x lock does not implement, and inheriting the
        1.3 hook is not an option because that version is not the one running.
        """

        when = config.get("when") if isinstance(config, Mapping) else None
        if when is None:
            return True
        request = SimpleNamespace(tool_call=tool_call, state=state, runtime=runtime)
        return bool(when(request))

    async def aafter_model(
        self, state: Any, runtime: Any
    ) -> dict[str, Any] | None:
        messages = state["messages"]
        if not messages:
            return None

        last_ai_msg = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, AIMessage)
            ),
            None,
        )
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        action_requests: list[Any] = []
        review_configs: list[Any] = []
        approval_indices: list[int] = []
        for index, tool_call in enumerate(last_ai_msg.tool_calls):
            config = self.interrupt_on.get(tool_call["name"])
            if config is None:
                continue
            if not self._should_interrupt(tool_call, config, state, runtime):
                continue
            action_request, review_config = self._create_action_and_config(
                tool_call, config, state, runtime
            )
            action_requests.append(action_request)
            review_configs.append(review_config)
            approval_indices.append(index)

        if not action_requests:
            return None

        control = _control_from_runtime(runtime)
        if control is None:
            raise RuntimeError("tool approval needs a live RunControl")
        decisions = await control.request_approval(
            self._approval_payload(action_requests, review_configs)
        )
        if len(decisions) != len(approval_indices):
            raise ValueError(
                f"Number of human decisions ({len(decisions)}) does not match "
                f"number of hanging tool calls ({len(approval_indices)})."
            )

        revised_tool_calls: list[Any] = []
        artificial_tool_messages: list[ToolMessage] = []
        decision_index = 0
        for index, tool_call in enumerate(last_ai_msg.tool_calls):
            if index not in approval_indices:
                revised_tool_calls.append(tool_call)
                continue
            config = self.interrupt_on[tool_call["name"]]
            decision = decisions[decision_index]
            decision_index += 1
            revised_tool_call, tool_message = self._process_decision(
                decision, tool_call, config
            )
            if revised_tool_call is not None:
                revised_tool_calls.append(revised_tool_call)
            if tool_message:
                artificial_tool_messages.append(tool_message)

        last_ai_msg.tool_calls = revised_tool_calls
        return {"messages": [last_ai_msg, *artificial_tool_messages]}

    def _approval_payload(
        self,
        action_requests: Sequence[Any],
        review_configs: Sequence[Any],
    ) -> dict[str, object]:
        metadata = self._metadata_by_name
        actions: list[dict[str, object]] = []
        for request, review in zip(
            action_requests, review_configs, strict=True
        ):
            name = str(_field(request, "name") or "")
            actions.append(
                {
                    "name": name,
                    "arguments": dict(_field(request, "args") or {}),
                    "tool_metadata": dict(metadata.get(name) or {}),
                    "description": str(_field(request, "description") or ""),
                    "allowed_decisions": list(
                        _field(review, "allowed_decisions") or ()
                    ),
                }
            )
        return {"approval_id": str(uuid.uuid4()), "actions": actions}


__all__ = ["ApprovalMiddleware"]
