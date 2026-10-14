from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from bianbu_agent.inventory.models import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
)
from bianbu_agent.plugins import ToolContribution
from bianbu_agent.tools.loader import DirectoryToolLoader
from bianbu_agent.tools.models import ToolLoadContext


@dataclass(frozen=True, slots=True)
class ToolDirectoryCandidate:
    tools: Mapping[str, ToolContribution]
    sources: Mapping[tuple[CapabilityKind, str], CapabilitySource]
    source_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


class ToolDirectoryCatalog:
    def __init__(self, loader: DirectoryToolLoader | None = None, *, include_builtins: bool = False) -> None:
        self._loader = loader or DirectoryToolLoader()
        self._include_builtins = include_builtins
        self._current: ToolDirectoryCandidate | None = None

    @property
    def current(self) -> ToolDirectoryCandidate | None:
        return self._current

    def build(
        self,
        *,
        workspace_root: Path,
        home_root: Path | None = None,
        services: Mapping[str, object] | None = None,
    ) -> ToolDirectoryCandidate:
        workspace = Path(workspace_root).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        home = Path(home_root or workspace.parent).expanduser().resolve()
        from bianbu_agent.tools.builtins import BUILTIN_MODULES, load_builtin

        builtin_names = set(BUILTIN_MODULES) if self._include_builtins else set()
        tools: dict[str, ToolContribution] = {}
        sources: dict[tuple[CapabilityKind, str], CapabilitySource] = {}
        digest = hashlib.sha256()
        contributions = []
        for name in sorted(builtin_names):
            contribution = load_builtin(name, context=ToolLoadContext(
                workspace_root=workspace, services={"home_root": home, **dict(services or {})}))
            contributions.append((Path(contribution.spec.metadata['source_path']).parent, contribution))
        for child, contribution in contributions:
            name = contribution.spec.name
            if name in tools:
                raise ValueError(f"duplicate Tool name: {name}")
            metadata = contribution.spec.metadata
            source = CapabilitySource(
                kind=CapabilityKind.TOOL,
                capability_id=name,
                version=str(metadata["version"]),
                root=child,
                manifest_path=metadata["source_path"],
                content_digest=str(metadata["content_digest"]),
                origin=CapabilityOrigin.STANDALONE,
                metadata={"directory_catalog": True, "builtin": bool(metadata.get("builtin"))},
            )
            tools[name] = contribution
            sources[(CapabilityKind.TOOL, name)] = source
            encoded_name = name.encode("utf-8")
            encoded_digest = source.content_digest.encode("ascii")
            digest.update(len(encoded_name).to_bytes(8, "big"))
            digest.update(encoded_name)
            digest.update(encoded_digest)
        candidate = ToolDirectoryCandidate(
            tools={name: tools[name] for name in sorted(tools)},
            sources={key: sources[key] for key in sorted(sources, key=lambda key: key[1])},
            source_digest=digest.hexdigest(),
        )
        self._current = candidate
        return candidate


__all__ = ["ToolDirectoryCandidate", "ToolDirectoryCatalog"]
