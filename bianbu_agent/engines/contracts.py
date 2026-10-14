from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from bianbu_agent.runs.control import RunControl
from bianbu_agent.runs.events import RunEvent


@runtime_checkable
class AgentEngine(Protocol):
    def stream(
        self,
        prompt: str | None,
        *,
        session_id: str,
        product_session_id: str | None = None,
        control: RunControl,
    ) -> AsyncIterator[RunEvent]: ...


__all__ = ["AgentEngine"]
