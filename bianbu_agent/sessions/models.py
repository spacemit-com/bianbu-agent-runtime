from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class StoredSession:
    """Persistence-neutral representation of one Session."""

    id: str
    title: str
    agent_id: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: float = 0
    updated_at: float = 0
    active_thread_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


__all__ = ["StoredSession"]
