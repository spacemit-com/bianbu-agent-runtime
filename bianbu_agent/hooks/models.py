from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType


from bianbu_agent.hooks.state import CommandSessionState


_CATALOG_PLUGIN_ID = "bianbu.catalog.filesystem"


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


@dataclass(frozen=True, slots=True)
class HookLoadContext:
    workspace_root: Path
    data_root: Path
    owner_plugin_id: str = _CATALOG_PLUGIN_ID
    services: Mapping[str, object] = field(default_factory=dict)
    config: Mapping[str, object] = field(default_factory=dict)
    command_state: CommandSessionState = field(default_factory=CommandSessionState)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workspace_root",
            Path(self.workspace_root).expanduser().resolve(),
        )
        object.__setattr__(
            self,
            "data_root",
            Path(self.data_root).expanduser().resolve(),
        )
        object.__setattr__(
            self,
            "owner_plugin_id",
            _required_text(self.owner_plugin_id, "owner_plugin_id"),
        )
        object.__setattr__(self, "services", MappingProxyType(dict(self.services)))
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))

    @property
    def workspace_path(self) -> Path:
        return self.workspace_root

    @property
    def data_path(self) -> Path:
        return self.data_root


__all__ = ["HookLoadContext"]
