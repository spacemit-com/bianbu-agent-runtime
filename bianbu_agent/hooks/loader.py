from __future__ import annotations

import re
from pathlib import Path

from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent.hooks.models import HookLoadContext
from bianbu_agent.inventory.models import CapabilityOrigin
from bianbu_agent.plugins.contributions import HookContribution


_CATALOG_PLUGIN_ID = "bianbu.catalog.filesystem"
_HOOK_ID = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)+")
_STANDALONE_VERSION = "0.0.0"


class DirectoryHookLoader:
    def __init__(self, *, catalog_plugin_id: str = _CATALOG_PLUGIN_ID) -> None:
        self._catalog_plugin_id = str(catalog_plugin_id).strip()

    def load(
        self,
        root: Path,
        *,
        context: HookLoadContext,
        origin: CapabilityOrigin = CapabilityOrigin.STANDALONE,
        plugin_id: str | None = None,
    ) -> HookContribution:
        origin = CapabilityOrigin(origin)
        resolved_root = self._resolved_root(root)
        hook_id, digest = self.inspect_single_file(resolved_root)
        if origin is not CapabilityOrigin.PLUGIN or not plugin_id:
            raise ValueError("Command Hooks must belong to a plugin")
        from bianbu_agent.hooks.command import load_command_hook
        return load_command_hook(resolved_root, context=context, owner=plugin_id, digest=digest)

    @staticmethod
    def inspect_single_file(root: Path) -> tuple[str, str]:
        """Validate a convention-based Hook without importing it."""

        resolved_root = DirectoryHookLoader._resolved_root(root)
        hook_id = resolved_root.name
        if _HOOK_ID.fullmatch(hook_id) is None:
            raise ValueError("Hook directory ID must be a stable dotted lowercase identifier")
        files = regular_files(resolved_root)
        relative_files = tuple(path.relative_to(resolved_root).as_posix() for path in files)
        if relative_files != ("hook.json",):
            raise ValueError("Hook package must contain only hook.json; scripts belong in plugin scripts/")
        from bianbu_agent.hooks.command import inspect_command_hook
        inspect_command_hook(resolved_root / "hook.json")
        return hook_id, digest_directory(resolved_root, files)

    @staticmethod
    def _resolved_root(root: Path) -> Path:
        source = Path(root).expanduser()
        if source.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {source}")
        return source.resolve()


__all__ = ["DirectoryHookLoader"]
