from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


EventKind = Literal[
    "run_started",
    "progress",
    "request_context",
    "hook_invoked",
    "hook_result",
    "reasoning",
    "token",
    "message_end",
    "tool_start",
    "tool_rejected",
    "tool_progress",
    "tool_end",
    "artifact",
    "usage",
    "context_compression",
    "input_applied",
    "approval_required",
    "approval_resolved",
    "done",
    "cancelled",
    "error",
]


@dataclass(frozen=True, slots=True)
class RunEvent:
    kind: EventKind
    payload: dict[str, Any]
