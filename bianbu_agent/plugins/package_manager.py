"""Management facade for self-contained namespaced Plugin packages."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

from bianbu_agent.home import AgentHome
from bianbu_agent.plugins.catalog import PluginCatalog
from bianbu_agent.plugins.installer import PluginInstaller
from bianbu_agent.plugins.models import (
    InstalledPlugin,
    PluginMember,
    PluginPackageIssue,
)
from bianbu_agent.plugins.composition import compose_plugin_config, load_plugin_overlays


T = TypeVar("T")


class PluginPackageManager:
    """Install Plugins and resolve their members only by qualified ID."""

    def __init__(self, home: AgentHome, *, file_only: bool = False) -> None:
        self._home = home
        self._installer = PluginInstaller(home, file_only=file_only)
        self._catalog = PluginCatalog(home)

    def install(self, source: str | Path, *, upgrade: bool = False) -> InstalledPlugin:
        return self._installer.install(Path(source), upgrade=upgrade)

    def list(self) -> tuple[InstalledPlugin, ...]:
        return self._installer.list()

    def scan(self) -> tuple[tuple[InstalledPlugin, ...], tuple[PluginPackageIssue, ...]]:
        return self._installer.scan()

    def inspect(self, plugin_id: str) -> InstalledPlugin | None:
        return self._installer.inspect(plugin_id)

    def verify(self, plugin_id: str) -> bool:
        return self._installer.verify(plugin_id)

    def package_root(self, plugin_id: str) -> Path:
        return self._installer.package_root(plugin_id)

    def assert_deletable(self, plugin_id: str) -> None:
        self._installer.assert_deletable(plugin_id)

    def uninstall(self, plugin_id: str) -> bool:
        return self._installer.uninstall(plugin_id)

    def discard_install(self, installed: InstalledPlugin) -> bool:
        return self._installer.discard_install(installed)

    def rollback(self, plugin_id: str) -> InstalledPlugin:
        return self._installer.rollback(plugin_id)

    def list_members(self, kind: str | None = None) -> tuple[PluginMember, ...]:
        return self._catalog.list(kind)

    def effective(
        self,
        runtime_configs: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], ...]:
        overlays = load_plugin_overlays(self._home.root / "plugins.yaml")
        runtime_configs = runtime_configs or {}
        views: list[dict[str, object]] = []
        installed = {plugin.id: plugin for plugin in self.list()}
        ordered: list[str] = []

        def visit(plugin_id: str, pending: set[str]) -> None:
            if plugin_id in ordered:
                return
            if plugin_id in pending:
                raise ValueError(f"Plugin dependency cycle includes {plugin_id}")
            pending.add(plugin_id)
            for dependency in installed[plugin_id].manifest.dependencies:
                if dependency.plugin_id in installed:
                    visit(dependency.plugin_id, pending)
            pending.remove(plugin_id)
            ordered.append(plugin_id)

        for plugin_id in sorted(installed):
            visit(plugin_id, set())
        for order, plugin_id in enumerate(ordered):
            plugin = installed[plugin_id]
            overlay = overlays.get(plugin.id)
            runtime_config = runtime_configs.get(plugin.id, {})
            if not isinstance(runtime_config, Mapping):
                raise ValueError(f"runtime Plugin config must be an object: {plugin.id}")
            effective = compose_plugin_config(
                plugin.manifest,
                runtime_config=runtime_config,
                home_config=overlay.config if overlay is not None else None,
            )
            views.append(
                {
                    "id": plugin.id,
                    "version": plugin.manifest.version,
                    "source": str(self.package_root(plugin.id)),
                    "generation_id": None,
                    "state": (
                        "configured"
                        if overlay is None or overlay.enabled
                        else "disabled"
                    ),
                    "dependency_order": order,
                    "enabled": overlay.enabled if overlay is not None else True,
                    "dependencies": [
                        {
                            "id": dependency.plugin_id,
                            "version": dependency.version,
                        }
                        for dependency in plugin.manifest.dependencies
                    ],
                    "config": dict(effective.config),
                    "provenance": dict(effective.provenance),
                    "members": [member.qualified_id for member in plugin.members],
                    "contributions": {
                        plural: sorted(
                            member.qualified_id
                            for member in plugin.members
                            if member.kind == kind
                        )
                        for plural, kind in (
                            ("tools", "tool"),
                            ("skills", "skill"),
                            ("hooks", "hook"),
                            ("mcps", "mcp"),
                        )
                    },
                }
            )
        return tuple(views)

    def resolve(self, qualified_id: str) -> PluginMember | None:
        return self._catalog.resolve(qualified_id)

    def with_open_member(
        self, qualified_id: str, consumer: Callable[[Path, PluginMember], T]
    ) -> T | None:
        return self._catalog.with_open_member(qualified_id, consumer)


__all__ = ["PluginPackageManager"]
