from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from bianbu_agent.plugins.models import (
    PluginFactory,
    PluginManifest,
)


@dataclass(frozen=True, slots=True)
class PluginSpec:
    id: str
    config: Mapping[str, object] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        plugin_id = str(self.id).strip()
        if not plugin_id:
            raise ValueError("PluginSpec.id must not be empty")
        object.__setattr__(self, "id", plugin_id)
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))


@dataclass(frozen=True, slots=True)
class PluginRegistration:
    manifest: PluginManifest
    factory: PluginFactory
    source: str = "explicit"

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PluginManifest):
            raise TypeError("manifest must be a PluginManifest")
        if not callable(self.factory):
            raise TypeError("factory must be callable")
        source = str(self.source).strip()
        if not source:
            raise ValueError("source must not be empty")
        object.__setattr__(self, "source", source)


__all__ = ["PluginRegistration", "PluginSpec"]
