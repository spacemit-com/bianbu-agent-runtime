"""Safe control-plane views which do not load Agent plugin code."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class EmptyGeneration:
    id: int = 0
    tools: object = MappingProxyType({})
    skills: object = MappingProxyType({})
    hooks: object = MappingProxyType({})
    mcps: object = MappingProxyType({})
    mcp_tools: object = MappingProxyType({})
    plugins: object = MappingProxyType({})
    statuses: object = MappingProxyType({})
    capability_catalog: object | None = None


class ControlPluginFacade:
    """Metadata-only plugin surface for API compatibility.

    Package execution, Tool/Skill/Hook loading and MCP startup belong to the
    Worker. The control process gets an empty immutable generation so legacy
    introspection routes cannot accidentally execute third-party code.
    """

    def __init__(self) -> None:
        self._generation = EmptyGeneration()

    @property
    def current_generation(self) -> EmptyGeneration:
        return self._generation

    async def start(self) -> EmptyGeneration:
        return self._generation

    async def close(self) -> None:
        return None

    async def health(self) -> dict[str, object]:
        return {}

    async def reload_installed(self, *, force: bool = False, handoff: bool = False):
        del force, handoff
        raise RuntimeError("control plane does not load or execute plugin packages; reload the Worker")

    @asynccontextmanager
    async def lease(self):
        yield self._generation


__all__ = ["ControlPluginFacade", "EmptyGeneration"]
