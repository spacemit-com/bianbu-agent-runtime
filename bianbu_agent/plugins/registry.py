from __future__ import annotations

import heapq
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points

from bianbu_agent.plugins.models import (
    PluginDiagnostic,
    PluginFactory,
    PluginManifest,
)


ENTRY_POINT_GROUP = "bianbu_agent.plugins"


class DuplicatePluginError(ValueError):
    """Raised when two registrations claim the same stable plugin ID."""


class PluginDependencyError(ValueError):
    """Raised when a requested plugin set cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class PluginCandidate:
    factory: PluginFactory
    manifest: PluginManifest
    source: str = "explicit"


class PluginRegistry:
    def __init__(self) -> None:
        self._candidates: dict[str, PluginCandidate] = {}

    @property
    def candidates(self) -> tuple[PluginCandidate, ...]:
        return tuple(self._candidates[key] for key in sorted(self._candidates))

    def register(
        self,
        factory: PluginFactory,
        manifest: PluginManifest,
        *,
        source: str = "explicit",
    ) -> PluginCandidate:
        if manifest.id in self._candidates:
            raise DuplicatePluginError(f"duplicate plugin ID: {manifest.id}")
        if not callable(factory):
            raise TypeError(f"plugin factory for {manifest.id} must be callable")
        candidate = PluginCandidate(factory=factory, manifest=manifest, source=source)
        self._candidates[manifest.id] = candidate
        return candidate

    def discover_entry_points(self) -> tuple[PluginDiagnostic, ...]:
        diagnostics: list[PluginDiagnostic] = []
        for entry_point in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda item: item.name):
            try:
                loaded = entry_point.load()
                factory, manifest = self._entry_point_registration(loaded)
                if manifest.id != entry_point.name:
                    raise ValueError(
                        f"entry point name {entry_point.name!r} does not match "
                        f"manifest ID {manifest.id!r}"
                    )
                self.register(factory, manifest, source="entry_point")
            except Exception as exc:
                diagnostics.append(
                    PluginDiagnostic(
                        code="entry_point_load_failed",
                        plugin_id=entry_point.name,
                        message=f"failed to load {entry_point.value}: {exc}",
                    )
                )
        return tuple(diagnostics)

    @staticmethod
    def _entry_point_registration(loaded: object) -> tuple[PluginFactory, PluginManifest]:
        if isinstance(loaded, tuple) and len(loaded) == 2:
            factory, manifest = loaded
        else:
            factory = loaded
            manifest = getattr(loaded, "manifest", None)
        if not callable(factory) or not isinstance(manifest, PluginManifest):
            raise TypeError(
                "entry point must load a (factory, PluginManifest) tuple or a "
                "callable with a PluginManifest 'manifest' attribute"
            )
        return factory, manifest

    def resolve(self, enabled_ids: Iterable[str]) -> tuple[PluginCandidate, ...]:
        enabled = frozenset(str(plugin_id) for plugin_id in enabled_ids)
        missing = sorted(enabled - self._candidates.keys())
        if missing:
            raise PluginDependencyError(f"plugins are not registered: {', '.join(missing)}")

        for plugin_id in sorted(enabled):
            manifest = self._candidates[plugin_id].manifest
            for dependency in manifest.dependencies:
                if dependency.plugin_id not in enabled:
                    raise PluginDependencyError(
                        f"dependency {dependency.plugin_id} required by {plugin_id} is not enabled"
                    )
                dependency_manifest = self._candidates[dependency.plugin_id].manifest
                if not dependency.accepts(dependency_manifest.version):
                    raise PluginDependencyError(
                        f"plugin {plugin_id} requires {dependency.plugin_id}{dependency.version}, "
                        f"found {dependency_manifest.version}"
                    )

        outgoing: dict[str, set[str]] = {plugin_id: set() for plugin_id in enabled}
        indegree: dict[str, int] = {plugin_id: 0 for plugin_id in enabled}
        for plugin_id in enabled:
            for dependency in self._candidates[plugin_id].manifest.dependencies:
                outgoing[dependency.plugin_id].add(plugin_id)
                indegree[plugin_id] += 1

        ready = [plugin_id for plugin_id, count in indegree.items() if count == 0]
        heapq.heapify(ready)
        ordered: list[str] = []
        while ready:
            plugin_id = heapq.heappop(ready)
            ordered.append(plugin_id)
            for dependent in sorted(outgoing[plugin_id]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, dependent)

        if len(ordered) != len(enabled):
            cycle_members = sorted(plugin_id for plugin_id, count in indegree.items() if count)
            raise PluginDependencyError(
                f"plugin dependency cycle detected: {', '.join(cycle_members)}"
            )
        return tuple(self._candidates[plugin_id] for plugin_id in ordered)


__all__ = [
    "DuplicatePluginError",
    "ENTRY_POINT_GROUP",
    "PluginCandidate",
    "PluginDependencyError",
    "PluginRegistry",
]
