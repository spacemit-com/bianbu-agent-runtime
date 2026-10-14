"""Async Agent registry backed by Manifest configuration packages."""

from __future__ import annotations

import asyncio

from bianbu_agent.agents import AgentPackageManager, AgentSpec, validate_agent_id
from bianbu_agent.home import AgentHome


class ManifestAgentStore:
    """Persist Agent declarations as one validated package directory each."""

    def __init__(
        self,
        home: AgentHome,
        package_manager: AgentPackageManager | None = None,
    ) -> None:
        self._home = home
        self._packages = package_manager or AgentPackageManager(home)
        if self._packages.home.root != home.root:
            raise ValueError("Agent package manager must use the same Bianbu Agent Home")
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        async with self._lock:
            self._packages.list()

    async def close(self) -> None:
        return None

    async def get(self, agent_id: str) -> AgentSpec | None:
        async with self._lock:
            package = self._packages.inspect(validate_agent_id(agent_id))
            return None if package is None else package.manifest.spec

    async def list(self) -> list[AgentSpec]:
        async with self._lock:
            return [package.manifest.spec for package in self._packages.list()]

    async def create(self, spec: AgentSpec) -> AgentSpec:
        async with self._lock:
            return self._packages.create(spec).manifest.spec

    async def update(self, spec: AgentSpec) -> AgentSpec:
        async with self._lock:
            return self._packages.update(spec).manifest.spec

    async def delete(self, agent_id: str) -> bool:
        async with self._lock:
            return self._packages.uninstall(validate_agent_id(agent_id))


__all__ = ["ManifestAgentStore"]
