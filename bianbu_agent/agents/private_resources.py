"""Capabilities that belong to exactly one Agent package.

An Agent directory may carry its own ``skills/``, ``mcps/`` and ``plugins/``
trees next to ``agent.yaml``.  Those entries are private to that Agent: they are
always materialized for it, they cannot be deselected in the editor, and when a
private entry shares an ID with a device-wide package the private one wins.

Discovery is deliberately read-only and executes nothing.  The returned
``InstalledCapability`` records use the same contract as the managed inventory,
so the Worker can overlay them on its Generation without a second loader.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent.inventory import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InstalledCapability,
)
from bianbu_agent.mcp.config import validate_mcp_config
from bianbu_agent.plugins.manifest import (
    APC_PLUGIN_MANIFEST_FILENAME,
    PLUGIN_MANIFEST_FILENAME,
    load_installed_plugin_manifest,
    plugin_definition_path,
)
from bianbu_agent.skills import DirectorySkillLoader


PRIVATE_KINDS = ("skills", "mcps", "plugins")
PRIVATE_METADATA_KEY = "agent_private"
OWNER_METADATA_KEY = "owner_agent_id"


@dataclass(frozen=True, slots=True)
class AgentPrivateResources:
    """Every private capability declared by one Agent package."""

    agent_id: str
    root: Path
    items: tuple[InstalledCapability, ...] = ()

    @property
    def skill_ids(self) -> tuple[str, ...]:
        return self._ids(CapabilityKind.SKILL)

    @property
    def mcp_ids(self) -> tuple[str, ...]:
        return self._ids(CapabilityKind.MCP)

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        return self._ids(CapabilityKind.PLUGIN)

    def _ids(self, kind: CapabilityKind) -> tuple[str, ...]:
        return tuple(
            sorted(
                item.source.capability_id
                for item in self.items
                if item.enabled and item.source.kind is kind
            )
        )

    def contains(self, kind: CapabilityKind, capability_id: str) -> bool:
        return any(
            item.source.kind is kind
            and item.source.capability_id == capability_id
            for item in self.items
        )

    @classmethod
    def empty(cls, agent_id: str) -> AgentPrivateResources:
        return cls(agent_id=agent_id, root=Path("/nonexistent"))


def discover_private_resources(root: Path, agent_id: str) -> AgentPrivateResources:
    """Read the private trees of one Agent package without executing them."""

    package_root = Path(root).expanduser().resolve()
    items: list[InstalledCapability] = []
    for kind, directory, definition in (
        (CapabilityKind.SKILL, "skills", "SKILL.md"),
        (CapabilityKind.MCP, "mcps", "mcp.json"),
        (CapabilityKind.PLUGIN, "plugins", PLUGIN_MANIFEST_FILENAME),
    ):
        tree = package_root / directory
        if not tree.exists():
            continue
        if tree.is_symlink() or not tree.is_dir():
            raise ValueError(f"Agent private {directory} must be a regular directory")
        definitions = (
            (definition, APC_PLUGIN_MANIFEST_FILENAME)
            if kind is CapabilityKind.PLUGIN
            else (definition,)
        )
        for child in sorted(tree.iterdir(), key=lambda item: item.name):
            if child.name.startswith("."):
                continue
            if child.is_symlink() or not child.is_dir():
                raise ValueError(
                    f"Agent private {directory} entries must be regular directories: {child}"
                )
            if not any((child / name).is_file() for name in definitions):
                continue
            items.append(_installed(kind, child, agent_id))
    # APM installs an Expert's MCP sub-resources as flat ``mcp/<name>.json``
    # documents in the submission format, not as ``mcps/<name>/mcp.json``.
    submitted_mcps = package_root / "mcp"
    if submitted_mcps.exists():
        if submitted_mcps.is_symlink() or not submitted_mcps.is_dir():
            raise ValueError("Agent private mcp must be a regular directory")
        for child in sorted(submitted_mcps.iterdir(), key=lambda item: item.name):
            if child.name.startswith("."):
                continue
            if child.is_symlink() or not child.is_file() or child.suffix != ".json":
                raise ValueError(
                    f"Agent private mcp entries must be regular .json files: {child}"
                )
            items.append(_installed_mcp_file(child, agent_id))
    unique: dict[tuple[CapabilityKind, str], InstalledCapability] = {}
    for item in items:
        unique.setdefault(
            (item.source.kind, item.source.capability_id), item
        )
    return AgentPrivateResources(
        agent_id=str(agent_id),
        root=package_root,
        items=tuple(unique.values()),
    )


def _installed_mcp_file(path: Path, agent_id: str) -> InstalledCapability:
    """Read one APM-submitted MCP file from an Agent package."""

    resolved = path.resolve()
    validate_mcp_config(resolved, workspace=resolved.parent.parent / "workspace")
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return InstalledCapability(
        source=CapabilitySource(
            kind=CapabilityKind.MCP,
            capability_id=resolved.stem,
            version="0.0.0",
            root=resolved.parent,
            manifest_path=resolved,
            content_digest=digest,
            origin=CapabilityOrigin.STANDALONE,
            metadata={
                "discovered": True,
                PRIVATE_METADATA_KEY: True,
                OWNER_METADATA_KEY: str(agent_id),
            },
        ),
        enabled=True,
    )


def _installed(kind: CapabilityKind, root: Path, agent_id: str) -> InstalledCapability:
    resolved = root.resolve()
    files = tuple(regular_files(resolved))
    if kind is CapabilityKind.SKILL:
        contribution = DirectorySkillLoader().load(resolved)
        capability_id = contribution.spec.name
        version = contribution.spec.version
        manifest_path = resolved / "SKILL.md"
        digest = str(
            contribution.spec.metadata.get("content_digest")
            or digest_directory(resolved, files)
        )
    elif kind is CapabilityKind.MCP:
        validate_mcp_config(resolved / "mcp.json", workspace=resolved.parent)
        capability_id = resolved.name
        version = "0.0.0"
        manifest_path = resolved / "mcp.json"
        digest = digest_directory(resolved, files)
    else:
        manifest = load_installed_plugin_manifest(resolved)
        if manifest.id != resolved.name:
            raise ValueError(
                "Agent private Plugin directory name must match plugin.id: "
                f"{resolved.name} != {manifest.id}"
            )
        capability_id = manifest.id
        version = manifest.version
        manifest_path = plugin_definition_path(resolved)
        digest = digest_directory(resolved, files)
    return InstalledCapability(
        source=CapabilitySource(
            kind=kind,
            capability_id=capability_id,
            version=version,
            root=resolved,
            manifest_path=manifest_path,
            content_digest=digest,
            origin=CapabilityOrigin.STANDALONE,
            metadata={
                "discovered": True,
                PRIVATE_METADATA_KEY: True,
                OWNER_METADATA_KEY: str(agent_id),
            },
        ),
        enabled=True,
    )


def overlay_private_resources(
    items: tuple[InstalledCapability, ...],
    private: AgentPrivateResources,
) -> tuple[InstalledCapability, ...]:
    """Return ``items`` with the Agent's private entries replacing same IDs."""

    if not private.items:
        return items
    shadowed = {
        (item.source.kind, item.source.capability_id) for item in private.items
    }
    kept = tuple(
        item
        for item in items
        if (item.source.kind, item.source.capability_id) not in shadowed
    )
    return (*kept, *private.items)


def is_private_capability(item: object) -> bool:
    source = getattr(item, "source", item)
    metadata = getattr(source, "metadata", {}) or {}
    return bool(metadata.get(PRIVATE_METADATA_KEY))


def owner_of(item: object) -> str:
    source = getattr(item, "source", item)
    metadata = getattr(source, "metadata", {}) or {}
    return str(metadata.get(OWNER_METADATA_KEY) or "")


def capability_owner(capability: object) -> str:
    """Owner Agent of a runtime contribution or catalog source, if private."""

    spec = getattr(capability, "spec", None)
    metadata = getattr(spec, "metadata", None)
    if isinstance(metadata, dict) or metadata is not None:
        owner = str((metadata or {}).get(OWNER_METADATA_KEY) or "")
        if owner:
            return owner
    metadata = getattr(capability, "metadata", None)
    if metadata is not None:
        return str((metadata or {}).get(OWNER_METADATA_KEY) or "")
    return ""


__all__ = [
    "AgentPrivateResources",
    "PRIVATE_KINDS",
    "PRIVATE_METADATA_KEY",
    "OWNER_METADATA_KEY",
    "capability_owner",
    "discover_private_resources",
    "is_private_capability",
    "overlay_private_resources",
    "owner_of",
]
