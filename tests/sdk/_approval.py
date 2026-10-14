"""Drive a Run's in-process approval from a test.

Tool approval no longer ends the Run.  The graph parks inside its approval
middleware, the Run stream carries ``approval_required`` while it is parked,
and the decision is handed back to that same Run.  A test that consumes the
stream therefore has to answer the approval, exactly like the console does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Callable

from bianbu_agent.runs.events import RunEvent


async def stream_answering_approvals(
    engine: Any,
    prompt: Any,
    *,
    control: Any,
    session_id: str,
    answer: Callable[[Mapping[str, object]], Sequence[Mapping[str, object]]],
    **kwargs: Any,
) -> list[RunEvent]:
    """Collect one engine stream, resolving every approval through ``answer``."""

    events: list[RunEvent] = []
    async for event in engine.stream(
        prompt, session_id=session_id, control=control, **kwargs
    ):
        events.append(event)
        if event.kind == "approval_required":
            decisions = answer(event.payload)
            if not control.resolve_approval(decisions):
                raise RuntimeError("approval was not parked on this Run")
    return events


__all__ = ["stream_answering_approvals"]
