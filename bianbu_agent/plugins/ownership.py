"""Private ownership records for capability directories installed by this SDK."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Literal

from bianbu_agent._internal.filesystem import UnsafeFilesystemEntry, regular_files


OWNERSHIP_FILENAME = ".bianbu-agent-sdk-ownership.json"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_KINDS = frozenset({"tool", "skill", "hook"})


@dataclass(frozen=True, slots=True)
class InstalledCapabilityRecord:
    """Deterministic ownership metadata for one installed capability."""

    kind: Literal["tool", "skill", "hook"]
    capability_id: str
    version: str
    content_digest: str
    origin: Literal["standalone", "plugin"] = "standalone"
    owner_plugin_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unsupported capability kind: {self.kind}")
        if not isinstance(self.capability_id, str) or not self.capability_id.strip():
            raise ValueError("capability_id must not be empty")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must not be empty")
        digest = str(self.content_digest).lower()
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError("content_digest must be a lowercase SHA-256 hex digest")
        if self.origin == "standalone" and self.owner_plugin_id is not None:
            raise ValueError("standalone ownership records must have no plugin owner")
        if self.origin == "plugin" and not self.owner_plugin_id:
            raise ValueError("plugin ownership records require an owner_plugin_id")
        if self.origin not in {"standalone", "plugin"}:
            raise ValueError("unsupported capability ownership origin")
        if self.schema_version != 1:
            raise ValueError("unsupported ownership record schema version")
        object.__setattr__(self, "capability_id", self.capability_id.strip())
        object.__setattr__(self, "version", self.version.strip())
        object.__setattr__(self, "content_digest", digest)

    def to_json_bytes(self) -> bytes:
        return (json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )

    @classmethod
    def from_path(cls, root: Path) -> InstalledCapabilityRecord:
        path = Path(root) / OWNERSHIP_FILENAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"invalid installed capability ownership metadata: {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("installed capability ownership metadata must be an object")
        expected = {
            "kind",
            "capability_id",
            "version",
            "content_digest",
            "origin",
            "owner_plugin_id", "schema_version",
        }
        if set(payload) != expected:
            raise ValueError(
                "installed capability ownership metadata has unsupported fields"
            )
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid installed capability ownership metadata") from exc


def write_ownership_record(root: Path, record: InstalledCapabilityRecord) -> Path:
    """Write deterministic private metadata after all owned files are present."""

    path = Path(root) / OWNERSHIP_FILENAME
    if path.exists() or path.is_symlink():
        raise ValueError("capability source contains reserved ownership metadata")
    path.write_bytes(record.to_json_bytes())
    return path


def installed_content_files(root: Path) -> tuple[Path, ...]:
    """Return owned payload files, excluding only the root ownership record."""

    resolved_root = Path(root).expanduser().resolve()
    files: list[Path] = []
    for path in regular_files(resolved_root):
        relative = path.relative_to(resolved_root)
        if path.name != OWNERSHIP_FILENAME:
            files.append(path)
            continue
        if relative.parts != (OWNERSHIP_FILENAME,):
            raise ValueError("nested reserved ownership metadata is not allowed")
    return tuple(files)


def reject_reserved_ownership_metadata(root: Path) -> None:
    """A source may never provide SDK-generated ownership metadata."""

    for path in regular_files(root):
        if path.name == OWNERSHIP_FILENAME:
            raise UnsafeFilesystemEntry(
                "capability source contains reserved ownership metadata"
            )


__all__ = [
    "InstalledCapabilityRecord",
    "OWNERSHIP_FILENAME",
    "installed_content_files",
    "reject_reserved_ownership_metadata",
    "write_ownership_record",
]
