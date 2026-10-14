from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType


_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


class CapabilityKind(str, Enum):
    PLUGIN = "plugin"
    MCP = "mcp"
    TOOL = "tool"
    SKILL = "skill"
    HOOK = "hook"


class CapabilityOrigin(str, Enum):
    STANDALONE = "standalone"
    PLUGIN = "plugin"


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


@dataclass(frozen=True, slots=True)
class CapabilitySource:
    kind: CapabilityKind
    capability_id: str
    version: str
    root: Path
    manifest_path: Path
    content_digest: str
    origin: CapabilityOrigin
    plugin_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = CapabilityKind(self.kind)
        origin = CapabilityOrigin(self.origin)
        capability_id = _required_text(self.capability_id, "capability_id")
        version = _required_text(self.version, "version")
        digest = _required_text(self.content_digest, "content_digest").lower()
        if _DIGEST_PATTERN.fullmatch(digest) is None:
            raise ValueError("content_digest must be a lowercase SHA-256 hex digest")
        root = Path(self.root).expanduser().resolve()
        manifest_path = Path(self.manifest_path).expanduser().resolve()
        if not manifest_path.is_relative_to(root):
            raise ValueError("manifest_path must be inside capability root")
        plugin_id = (
            _required_text(self.plugin_id, "plugin_id")
            if self.plugin_id is not None
            else None
        )
        if origin is CapabilityOrigin.PLUGIN and plugin_id is None:
            raise ValueError("plugin_id is required for plugin-owned capability")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "content_digest", digest)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "manifest_path", manifest_path)
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class InstalledCapability:
    source: CapabilitySource
    enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", bool(self.enabled))


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    revision: int
    items: tuple[InstalledCapability, ...] = ()

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("revision must not be negative")
        object.__setattr__(self, "items", tuple(self.items))


__all__ = [
    "CapabilityKind",
    "CapabilityOrigin",
    "CapabilitySource",
    "InstalledCapability",
    "InventorySnapshot",
]
