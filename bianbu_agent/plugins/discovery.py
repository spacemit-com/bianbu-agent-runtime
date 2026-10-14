from __future__ import annotations

import logging
from pathlib import Path
from bianbu_agent.mcp.plugin import MCPAuthorizer
from bianbu_agent._internal.filesystem import UnsafeFilesystemEntry

from bianbu_agent.inventory.models import CapabilityKind, InventorySnapshot
from bianbu_agent.plugins.directory import DirectoryPackagePlugin
from bianbu_agent.plugins.manifest import (
    load_installed_plugin_manifest,
    plugin_definition_path,
)
from bianbu_agent.plugins.models import (
    PluginFactory,
    PluginManifest,
)
from bianbu_agent.plugins.registry import PluginCandidate


logger = logging.getLogger(__name__)


class DirectoryPluginDiscovery:
    """Create runtime containers for enabled convention-based Plugins."""

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        mcp_authorizer: MCPAuthorizer | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()
        self._mcp_authorizer = mcp_authorizer

    def discover(self, snapshot: InventorySnapshot) -> tuple[PluginCandidate, ...]:
        candidates: list[PluginCandidate] = []
        for installed in sorted(
            snapshot.items,
            key=lambda item: (item.source.kind.value, item.source.capability_id),
        ):
            if not installed.enabled or installed.source.kind is not CapabilityKind.PLUGIN:
                continue
            root = installed.source.root
            plugin_id = installed.source.capability_id
            try:
                manifest = load_installed_plugin_manifest(root)
            except UnsafeFilesystemEntry:
                # A redirectable package entry is an attack, not a bad file.
                raise
            except ValueError as exc:
                # One unreadable package must not stop the whole generation.
                logger.warning(
                    "Skipping unusable Plugin %s at %s: %s", plugin_id, root, exc
                )
                continue
            if (
                manifest.id != plugin_id
                or installed.source.manifest_path != plugin_definition_path(root).resolve()
                or installed.source.version != manifest.version
            ):
                logger.warning(
                    "Skipping Plugin %s at %s: package does not match its Inventory entry",
                    plugin_id,
                    root,
                )
                continue
            factory = self._package_factory(manifest, root)
            candidates.append(
                PluginCandidate(
                    factory=factory,
                    manifest=manifest,
                    source=f"directory:{root}",
                )
            )
        from bianbu_agent.mcp.sources import MCPSourceCatalog

        candidates.extend(
            MCPSourceCatalog.from_snapshot(snapshot).candidates(
                workspace=self._workspace_root,
                authorize=self._mcp_authorizer,
            )
        )
        return tuple(candidates)

    def _package_factory(self, manifest: PluginManifest, root: Path) -> PluginFactory:
        def create_plugin(_context):
            return DirectoryPackagePlugin(
                manifest,
                root=root,
                workspace=self._workspace_root,
            )

        create_plugin.manifest = manifest  # type: ignore[attr-defined]
        return create_plugin
__all__ = ["DirectoryPluginDiscovery"]
