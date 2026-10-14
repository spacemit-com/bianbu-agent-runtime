"""Real runtime adapter for one manifest-backed directory Plugin."""

from __future__ import annotations

import inspect
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent._internal.importing import load_package_module
from bianbu_agent.hooks import DirectoryHookLoader, HookLoadContext
from bianbu_agent.inventory import CapabilityOrigin
from bianbu_agent.plugins.models import PluginContext, PluginHealth, PluginManifest
from bianbu_agent.skills import DirectorySkillLoader
from bianbu_agent.tools import DirectoryToolLoader, ToolLoadContext


class DirectoryPackagePlugin:
    """Load the contributions found in one Plugin package directory."""

    def __init__(self, manifest: PluginManifest, *, root: Path, workspace: Path) -> None:
        self.manifest = manifest
        self._root = Path(root).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self.stopped = False
        self._command_snapshot = None
        from bianbu_agent.hooks.state import CommandSessionState
        self._command_state = CommandSessionState()

    async def setup(self, context: PluginContext) -> None:
        if context.plugin_id != self.manifest.id:
            raise ValueError("Plugin context ID does not match directory manifest")
        assert context.scope is not None
        services = context.scope.service_view
        for name in self.manifest.contributions.tools:
            contribution = DirectoryToolLoader().load(
                    self._root / "tools" / name,
                    context=ToolLoadContext(
                        config=context.config,
                        workspace_root=self._workspace,
                        services=services,
                    ),
                    plugin_id=self.manifest.id,
                    data_root=context.data_dir,
                )
            context.scope.register_tool(
                replace(
                    contribution,
                    spec=replace(
                        contribution.spec,
                        metadata={
                            **dict(contribution.spec.metadata),
                            "qualified_id": f"{self.manifest.id}/{name}",
                        },
                    ),
                )
            )
        for name in self.manifest.contributions.skills:
            contribution = DirectorySkillLoader().load(
                    self._root / "skills" / name,
                    origin=CapabilityOrigin.PLUGIN,
                    plugin_id=self.manifest.id,
                )
            context.scope.register_skill(
                replace(
                    contribution,
                    spec=replace(
                        contribution.spec,
                        metadata={
                            **dict(contribution.spec.metadata),
                            "qualified_id": f"{self.manifest.id}/{name}",
                        },
                    ),
                )
            )
        hook_root = self._root
        if any((self._root / "hooks" / name / "hook.json").is_file()
               for name in self.manifest.contributions.hooks):
            # Keep external scripts and their imports fixed for this leased
            # plugin generation, even if the installed directory is replaced.
            self._command_snapshot = tempfile.TemporaryDirectory(
                prefix="command-hooks-", dir=context.data_dir)
            hook_root = Path(self._command_snapshot.name)
            for source in regular_files(self._root):
                target = hook_root / source.relative_to(self._root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        for name in self.manifest.contributions.hooks:
            context.scope.register_hook(
                DirectoryHookLoader().load(
                    hook_root / "hooks" / name,
                    origin=CapabilityOrigin.PLUGIN,
                    plugin_id=self.manifest.id,
                    context=HookLoadContext(
                        config=context.config,
                        workspace_root=self._workspace,
                        data_root=context.data_dir,
                        owner_plugin_id=self.manifest.id,
                        command_state=self._command_state,
                        services=services,
                    ),
                )
            )
        if self.manifest.entrypoint:
            filename, _, callable_name = self.manifest.entrypoint.partition(":")
            digest = digest_directory(self._root, regular_files(self._root))
            module = load_package_module(
                self._root,
                Path(filename).stem,
                namespace=(
                    f"_bianbu_agent_plugin_{self.manifest.id.replace('.', '_')}_{digest}"
                ),
            )
            activate = getattr(module, callable_name, None)
            if not callable(activate):
                raise TypeError(f"Plugin entrypoint is not callable: {self.manifest.entrypoint}")
            result = activate(context)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                raise TypeError("Plugin entrypoint must return None")

    async def start(self) -> None:
        self.stopped = False
        return None

    async def stop(self) -> None:
        self.stopped = True
        self._command_state.clear()
        if self._command_snapshot is not None:
            self._command_snapshot.cleanup()
            self._command_snapshot = None
        return None

    async def health(self) -> PluginHealth:
        return PluginHealth.healthy()

    async def reconfigure(self, _config: Mapping[str, object]) -> None:
        return None


__all__ = ["DirectoryPackagePlugin"]
