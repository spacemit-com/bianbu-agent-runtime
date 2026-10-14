from __future__ import annotations

from dataclasses import dataclass

from bianbu_agent.runs.events import RunEvent


@dataclass(frozen=True, slots=True)
class StoredRun:
    """Persistence-neutral representation of one Run."""

    request_id: str
    session_id: str
    status: str
    request: dict[str, object]
    events: tuple[RunEvent, ...]
    result: object | None
    created_at: float = 0
    updated_at: float = 0


__all__ = ["StoredRun"]
