"""Framework-neutral Plugin package contracts and lazy management exports."""

from __future__ import annotations

from importlib import import_module

from bianbu_agent.plugins.contributions import (
    ContextPatch,
    DEFAULT_ACCOUNT_ID,
    HookContribution,
    HookPoint,
    MCPRuntimeContribution,
    PluginContributions,
    RunContext,
    SkillContribution,
    SkillSpec,
    skill_unavailable_reason,
    ToolCallContext,
    ToolContribution,
    ToolResult,
    ToolSpec,
)
from bianbu_agent.plugins.models import (
    InstalledPlugin,
    Plugin,
    PluginConfigError,
    PluginContributionManifest,
    PluginContext,
    PluginDependency,
    PluginDiagnostic,
    PluginFactory,
    PluginHealth,
    PluginManifest,
    PluginMember,
    PluginPackageIssue,
    PluginState,
)


_LAZY = {
    "CapabilityInstaller": "bianbu_agent.plugins.installer",
    "CapabilityReloadError": "bianbu_agent.plugins.manager",
    "ContributionConflictError": "bianbu_agent.plugins.host",
    "DirectoryPluginDiscovery": "bianbu_agent.plugins.discovery",
    "DuplicatePluginError": "bianbu_agent.plugins.registry",
    "InstalledCapabilityRecord": "bianbu_agent.plugins.ownership",
    "MCPRuntimeView": "bianbu_agent.plugins.host",
    "PluginCandidate": "bianbu_agent.plugins.registry",
    "PluginCatalog": "bianbu_agent.plugins.catalog",
    "PluginDependencyError": "bianbu_agent.plugins.registry",
    "PluginGeneration": "bianbu_agent.plugins.host",
    "PluginHost": "bianbu_agent.plugins.host",
    "PluginInstaller": "bianbu_agent.plugins.installer",
    "PluginManager": "bianbu_agent.plugins.manager",
    "PluginPackageManager": "bianbu_agent.plugins.package_manager",
    "PluginRegistration": "bianbu_agent.plugins.specs",
    "PluginRegistry": "bianbu_agent.plugins.registry",
    "PluginReloadResult": "bianbu_agent.plugins.manager",
    "PluginRuntime": "bianbu_agent.plugins.host",
    "PluginSpec": "bianbu_agent.plugins.specs",
    "PluginStatus": "bianbu_agent.plugins.host",
    "PluginScope": "bianbu_agent.plugins.scope",
}


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "ContextPatch",
    "DEFAULT_ACCOUNT_ID",
    "HookContribution",
    "HookPoint",
    "MCPRuntimeContribution",
    "InstalledPlugin",
    "Plugin",
    "PluginConfigError",
    "PluginContributionManifest",
    "PluginContext",
    "PluginContributions",
    "PluginDependency",
    "PluginDiagnostic",
    "PluginFactory",
    "PluginHealth",
    "PluginManifest",
    "PluginMember",
    "PluginPackageIssue",
    "PluginState",
    "RunContext",
    "SkillContribution",
    "SkillSpec",
    "skill_unavailable_reason",
    "ToolCallContext",
    "ToolContribution",
    "ToolResult",
    "ToolSpec",
    *_LAZY,
]
