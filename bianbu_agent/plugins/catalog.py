"""Read-only catalog for capabilities contained in installed Plugin packages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

from bianbu_agent.home import AgentHome
from bianbu_agent.plugins.installer import PluginInstaller
from bianbu_agent.plugins.models import PluginMember


CapabilityKind = Literal["tool", "skill", "hook", "mcp"]
_KINDS = frozenset({"tool", "skill", "hook", "mcp"})
T = TypeVar("T")


class PluginCatalog:
    """Resolve only explicit ``<plugin-id>/<local-name>`` capability IDs."""

    def __init__(self, home: AgentHome) -> None:
        self._installer = PluginInstaller(home)

    def list(self, kind: CapabilityKind | str | None = None) -> tuple[PluginMember, ...]:
        normalized_kind = self._normalize_kind(kind) if kind is not None else None
        members = (
            member
            for plugin in self._installer.list()
            for member in plugin.members
            if normalized_kind is None or member.kind == normalized_kind
        )
        return tuple(sorted(members, key=lambda item: (item.kind, item.qualified_id)))

    def resolve(self, capability_id: str) -> PluginMember | None:
        namespace, separator, local_name = str(capability_id).strip().partition("/")
        if not separator or not namespace or not local_name or "/" in local_name:
            return None
        for member in self.list():
            if member.namespace == namespace and member.local_name == local_name:
                return member
        return None

    def with_open_member(
        self, capability_id: str, consumer: Callable[[Path, PluginMember], T]
    ) -> T | None:
        """Consume a member while its validated directory descriptor remains open."""

        return self._installer.with_open_member(capability_id, consumer)

    @staticmethod
    def _normalize_kind(kind: CapabilityKind | str) -> CapabilityKind:
        if kind not in _KINDS:
            raise ValueError(f"unsupported capability kind: {kind}")
        return kind  # type: ignore[return-value]


__all__ = ["PluginCatalog"]
