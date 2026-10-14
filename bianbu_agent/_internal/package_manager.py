"""Shared implementation for standalone capability package managers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from bianbu_agent.home import AgentHome

if TYPE_CHECKING:
    from bianbu_agent.plugins.ownership import InstalledCapabilityRecord


CapabilityKind = Literal["skill"]


class StandalonePackageManager:
    """Manage one kind of standalone package in a prepared Bianbu Agent Home."""

    def __init__(self, home: AgentHome, kind: CapabilityKind, *, file_only: bool = False) -> None:
        from bianbu_agent.plugins.installer import CapabilityInstaller

        self._kind = kind
        self._installer = CapabilityInstaller(home, file_only=file_only)

    def install(
        self,
        source: str | Path,
        *,
        upgrade: bool = False,
    ) -> InstalledCapabilityRecord:
        return self._installer.install_skill(Path(source), upgrade=upgrade)

    def list(self) -> tuple[InstalledCapabilityRecord, ...]:
        return self._installer.list(self._kind)

    def inspect(self, package_id: str) -> InstalledCapabilityRecord | None:
        return self._installer.inspect(self._kind, package_id)

    def verify(self, package_id: str) -> bool:
        return self._installer.verify(self._kind, package_id)

    def uninstall(self, package_id: str) -> bool:
        return self._installer.uninstall(self._kind, package_id)


__all__ = ["StandalonePackageManager"]
