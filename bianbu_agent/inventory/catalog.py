from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent.inventory.models import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InventorySnapshot,
)
from bianbu_agent.plugins import (
    SkillContribution,
    ToolContribution,
)
from bianbu_agent.skills import DirectorySkillLoader
from bianbu_agent.tools import DirectoryToolLoader, ToolDirectoryCatalog


class CapabilityConflictError(ValueError):
    """Raised when two enabled sources claim one global capability name."""


@dataclass(frozen=True, slots=True)
class CapabilityCatalog:
    tools: Mapping[str, ToolContribution]
    skills: Mapping[str, SkillContribution]
    sources: Mapping[tuple[CapabilityKind, str], CapabilitySource]
    inventory_revision: int
    plugin_sources: Mapping[str, CapabilitySource] = field(default_factory=dict)
    mcp_sources: Mapping[str, CapabilitySource] = field(default_factory=dict)
    #: Capabilities that belong to one Agent package rather than the device.
    #: Keyed by ``(kind, capability_id)``; the value is the owning Agent ID.
    private_owners: Mapping[tuple[CapabilityKind, str], str] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        object.__setattr__(self, "skills", MappingProxyType(dict(self.skills)))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        object.__setattr__(
            self,
            "plugin_sources",
            MappingProxyType(dict(self.plugin_sources)),
        )
        object.__setattr__(
            self,
            "mcp_sources",
            MappingProxyType(dict(self.mcp_sources)),
        )
        object.__setattr__(
            self,
            "private_owners",
            MappingProxyType(dict(self.private_owners)),
        )
        if self.inventory_revision < 0:
            raise ValueError("inventory_revision must not be negative")


@dataclass(frozen=True, slots=True)
class PreparedCapabilityCatalog:
    """Catalog state that is ready before the builtin Tool list exists."""

    skills: Mapping[str, SkillContribution]
    sources: Mapping[tuple[CapabilityKind, str], CapabilitySource]
    bundled_tools: Mapping[str, ToolContribution]
    bundled_sources: Mapping[tuple[CapabilityKind, str], CapabilitySource]
    inventory_revision: int
    plugin_sources: Mapping[str, CapabilitySource]
    mcp_sources: Mapping[str, CapabilitySource]
    private_owners: Mapping[tuple[CapabilityKind, str], str]


class CapabilityCatalogBuilder:
    def __init__(
        self,
        *,
        workspace_root: Path,
        tool_loader: DirectoryToolLoader | None = None,
        skill_loader: DirectorySkillLoader | None = None,
        tool_services: Mapping[str, object] | None = None,
        home_root: Path | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._tool_loader = tool_loader or DirectoryToolLoader()
        self._tool_catalog = ToolDirectoryCatalog(self._tool_loader, include_builtins=True)
        self._home_root = Path(
            home_root or self._workspace_root.parent
        ).expanduser().resolve()
        self._skill_loader = skill_loader or DirectorySkillLoader()
        self._tool_services = dict(tool_services or {})

    def build(self, snapshot: InventorySnapshot) -> CapabilityCatalog:
        """Build the whole catalog synchronously (tests and Control callers)."""

        prepared = self.prepare(snapshot)
        return self.finish(prepared, self.build_tools())

    def prepare(self, snapshot: InventorySnapshot) -> PreparedCapabilityCatalog:
        """Resolve everything the catalog needs except the builtin Tool list.

        The builtin Tool catalog constructs one LangChain Tool per module and
        dominates this call.  Callers that can overlap CPU work with an I/O
        wait (a Worker starting its MCP servers) split it out through
        :meth:`build_tools` and :meth:`finish`.
        """

        self.verify_snapshot(snapshot)
        bundled_tools: dict[str, ToolContribution] = {}
        bundled_sources: dict[tuple[CapabilityKind, str], CapabilitySource] = {}
        skills: dict[str, SkillContribution] = {}
        sources: dict[tuple[CapabilityKind, str], CapabilitySource] = {}
        # Tools live inside the atomic Agent package, so removal leaves no
        # separate registration or copied payload to reconcile.
        from bianbu_agent.agents import AgentPackageManager
        from bianbu_agent.agents.bundled_tools import load_bundled_tools
        from bianbu_agent.home import AgentHome

        for package in AgentPackageManager(AgentHome(self._home_root)).list():
            for name, contribution in load_bundled_tools(
                package.root, package.manifest,
                workspace_root=self._workspace_root, services=self._tool_services,
            ).items():
                self._add(bundled_tools, name, contribution, "tool")
                bundled_sources[(CapabilityKind.TOOL, name)] = self._source_from_tool(
                    contribution, name
                )
        plugin_sources: dict[str, CapabilitySource] = {}
        mcp_sources: dict[str, CapabilitySource] = {}
        private_owners: dict[tuple[CapabilityKind, str], str] = {}
        enabled = sorted(
            (item for item in snapshot.items if item.enabled),
            key=lambda item: (item.source.kind.value, item.source.capability_id),
        )
        for item in enabled:
            kind = item.source.kind
            owner = str(item.source.metadata.get("owner_agent_id") or "")
            if item.source.metadata.get("agent_private") and owner:
                private_owners[(kind, item.source.capability_id)] = owner
            if kind is CapabilityKind.TOOL:
                continue
            elif kind is CapabilityKind.SKILL:
                contribution = self._skill_loader.load(item.source.root)
                if contribution.spec.name != item.source.capability_id:
                    raise ValueError(
                        f"Skill Inventory ID {item.source.capability_id!r} does not "
                        f"match Manifest name {contribution.spec.name!r}"
                    )
                self._add(skills, contribution.spec.name, contribution, "skill")
                sources[(kind, contribution.spec.name)] = self._source_from_skill(
                    contribution
                )
            elif kind is CapabilityKind.PLUGIN:
                plugin_sources[item.source.capability_id] = item.source
            elif kind is CapabilityKind.MCP:
                from bianbu_agent.mcp.sources import runtime_mcp_id

                mcp_sources[runtime_mcp_id(item.source.capability_id)] = item.source
                sources[(kind, item.source.capability_id)] = item.source
        return PreparedCapabilityCatalog(
            skills=skills,
            sources=sources,
            bundled_tools=bundled_tools,
            bundled_sources=bundled_sources,
            inventory_revision=snapshot.revision,
            plugin_sources=plugin_sources,
            mcp_sources=mcp_sources,
            private_owners=private_owners,
        )

    def build_tools(self) -> object:
        """Construct the builtin Tool contributions (the CPU-heavy half)."""

        return self._tool_catalog.build(
            workspace_root=self._workspace_root,
            home_root=self._home_root,
            services=self._tool_services,
        )

    def finish(self, prepared: PreparedCapabilityCatalog, tool_candidate) -> CapabilityCatalog:
        """Combine the resolved sources with the builtin Tool candidate."""

        tools: dict[str, ToolContribution] = dict(tool_candidate.tools)
        sources: dict[tuple[CapabilityKind, str], CapabilitySource] = dict(
            tool_candidate.sources
        )
        for name, contribution in prepared.bundled_tools.items():
            self._add(tools, name, contribution, "tool")
        sources.update(prepared.bundled_sources)
        sources.update(prepared.sources)
        return CapabilityCatalog(
            tools={name: tools[name] for name in sorted(tools)},
            skills={
                name: prepared.skills[name] for name in sorted(prepared.skills)
            },
            sources={key: sources[key] for key in sorted(sources, key=self._source_key)},
            inventory_revision=prepared.inventory_revision,
            plugin_sources=prepared.plugin_sources,
            mcp_sources=prepared.mcp_sources,
            private_owners=prepared.private_owners,
        )

    @staticmethod
    def verify_snapshot(snapshot: InventorySnapshot) -> None:
        for item in snapshot.items:
            if not item.enabled:
                continue
            if item.source.kind is CapabilityKind.TOOL:
                continue
            root = item.source.root
            files = tuple(
                path
                for path in regular_files(root)
                if path.relative_to(root).parts[:1] != ("data",)
            )
            actual = digest_directory(root, files)
            if actual != item.source.content_digest:
                raise ValueError(
                    f"{item.source.kind.value} {item.source.capability_id} "
                    "content digest does not match Inventory"
                )

    @staticmethod
    def _add(target: dict, name: str, contribution: object, kind: str) -> None:
        if name in target:
            raise CapabilityConflictError(f"duplicate {kind} name: {name}")
        target[name] = contribution

    @staticmethod
    def _source_from_tool(
        contribution: ToolContribution, capability_id: str | None = None
    ) -> CapabilitySource:
        metadata = contribution.spec.metadata
        return CapabilitySource(
            kind=CapabilityKind.TOOL,
            capability_id=capability_id or contribution.spec.name,
            version=metadata["version"],
            root=Path(metadata["manifest_path"]).parent,
            manifest_path=metadata["manifest_path"],
            content_digest=metadata["content_digest"],
            origin=CapabilityOrigin(metadata["origin"]),
            plugin_id=metadata["plugin_id"],
            metadata={"owner_agent_id": metadata["owner_agent_id"]}
            if "owner_agent_id" in metadata else {},
        )

    @staticmethod
    def _source_from_skill(
        contribution: SkillContribution, capability_id: str | None = None
    ) -> CapabilitySource:
        metadata = contribution.spec.metadata
        return CapabilitySource(
            kind=CapabilityKind.SKILL,
            capability_id=capability_id or contribution.spec.name,
            version=metadata["version"],
            root=Path(metadata["manifest_path"]).parent,
            manifest_path=metadata["manifest_path"],
            content_digest=metadata["content_digest"],
            origin=CapabilityOrigin(metadata["origin"]),
            plugin_id=metadata["plugin_id"],
        )

    @staticmethod
    def _source_key(key: tuple[CapabilityKind, str]) -> tuple[str, str]:
        return key[0].value, key[1]


__all__ = [
    "CapabilityCatalog",
    "CapabilityCatalogBuilder",
    "CapabilityConflictError",
    "PreparedCapabilityCatalog",
]
