"""First-class MCP source records and internal lifecycle candidates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping
from bianbu_agent.mcp.plugin import MCPAuthorizer

from bianbu_agent.inventory import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InventorySnapshot,
)
from bianbu_agent.plugins.models import PluginManifest
from bianbu_agent.plugins.registry import PluginCandidate


def runtime_mcp_id(public_id: str) -> str:
    normalized = str(public_id).strip()
    if not normalized:
        raise ValueError("MCP public ID must not be empty")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"mcp.runtime-{digest}"


@dataclass(frozen=True, slots=True)
class MCPSourceRecord:
    id: str
    runtime_id: str
    root: Path
    content_digest: str
    source_kind: str
    owner_plugin_id: str | None
    source: CapabilitySource


class MCPSourceCatalog:
    def __init__(self, records: Mapping[str, MCPSourceRecord]) -> None:
        self.records = MappingProxyType(dict(records))

    @classmethod
    def from_snapshot(cls, snapshot: InventorySnapshot) -> MCPSourceCatalog:
        records: dict[str, MCPSourceRecord] = {}
        for item in sorted(
            snapshot.items,
            key=lambda candidate: candidate.source.capability_id,
        ):
            source = item.source
            if not item.enabled or source.kind is not CapabilityKind.MCP:
                continue
            public_id = source.capability_id
            expected = (source.root / "mcp.json").resolve()
            if (
                source.root.is_symlink()
                or source.manifest_path != expected
                or expected.is_symlink()
                or not expected.is_file()
            ):
                raise ValueError(f"MCP source {public_id} is not backed by mcp.json")
            if source.origin is CapabilityOrigin.PLUGIN:
                assert source.plugin_id is not None
                expected_id = f"{source.plugin_id}/{source.root.name}"
                if public_id != expected_id:
                    raise ValueError(
                        f"Plugin MCP ID {public_id!r} must be {expected_id!r}"
                    )
                source_kind = "plugin"
            else:
                if public_id != source.root.name:
                    raise ValueError(
                        f"standalone MCP ID {public_id!r} must match directory name"
                    )
                source_kind = "system" if source.metadata.get("system_managed") else "standalone"
            if public_id in records:
                raise ValueError(f"duplicate MCP source ID: {public_id}")
            records[public_id] = MCPSourceRecord(
                id=public_id,
                runtime_id=runtime_mcp_id(public_id),
                root=source.root,
                content_digest=source.content_digest,
                source_kind=source_kind,
                owner_plugin_id=source.plugin_id,
                source=source,
            )
        return cls(records)

    def candidates(
        self,
        *,
        workspace: Path,
        authorize: MCPAuthorizer | None = None,
    ) -> tuple[PluginCandidate, ...]:
        selected: list[PluginCandidate] = []
        for record in self.records.values():
            manifest = PluginManifest(
                id=record.runtime_id,
                version="0.0.0",
            )

            def create_plugin(_context, *, _record=record, _manifest=manifest):
                from bianbu_agent.mcp.plugin import MCPDirectoryPlugin

                return MCPDirectoryPlugin(
                    _manifest,
                    root=_record.root,
                    workspace=workspace,
                    mcp_id=_record.id,
                    owner_plugin_id=_record.owner_plugin_id,
                    source_kind=_record.source_kind,
                    authorize=authorize,
                )

            create_plugin.manifest = manifest  # type: ignore[attr-defined]
            selected.append(
                PluginCandidate(
                    factory=create_plugin,
                    manifest=manifest,
                    source=f"mcp:{record.id}",
                )
            )
        return tuple(selected)


__all__ = [
    "MCPSourceCatalog",
    "MCPSourceRecord",
    "runtime_mcp_id",
]
