from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from bianbu_agent._internal.filesystem import digest_directory
from bianbu_agent._internal.yaml_documents import load_strict_yaml
from bianbu_agent.inventory.models import CapabilityOrigin
from bianbu_agent.plugins import SkillContribution, SkillSpec
from bianbu_agent.plugins.ownership import installed_content_files


_CATALOG_PLUGIN_ID = "bianbu.catalog.filesystem"
_FRONTMATTER_FIELDS = {
    "name",
    "display_name",
    "description",
    "version",
    # Power format v2 display fields. They are optional here: a Skill written
    # before v2, or one from a vendor package, still loads and simply falls
    # back to ``name``.
    "name_zh",
    "name_en",
    "description_zh",
}


def _parse_skill(text: str) -> tuple[dict[str, str], str]:
    """Read one SKILL.md frontmatter block and its instructions.

    The frontmatter is YAML, so folded scalars (``>-``) and nested blocks such
    as ``metadata:`` are read by the parser instead of being rejected as
    unknown lines.  Only the fields this Runtime consumes are kept: a Skill from
    the public catalog may legitimately carry extra keys, and refusing the whole
    Skill for them would hide every neighbour in the same Plugin.
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("SKILL.md frontmatter is not closed") from exc
    try:
        document = load_strict_yaml("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid SKILL.md frontmatter: {exc}") from exc
    if document is None:
        document = {}
    if not isinstance(document, Mapping):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")
    metadata: dict[str, str] = {}
    for key in _FRONTMATTER_FIELDS:
        if key not in document:
            continue
        value = document[key]
        if value is None:
            continue
        if isinstance(value, (Mapping, list, tuple, set)):
            raise ValueError(f"SKILL.md frontmatter {key} must be a single string")
        metadata[key] = value if isinstance(value, str) else str(value)
    if not metadata.get("name", "").strip():
        raise ValueError("SKILL.md frontmatter name is required")
    if not metadata.get("description", "").strip():
        raise ValueError("SKILL.md frontmatter description is required")
    instructions = "\n".join(lines[end + 1 :]).strip()
    return metadata, instructions


class DirectorySkillLoader:
    def __init__(self, *, catalog_plugin_id: str = _CATALOG_PLUGIN_ID) -> None:
        self._catalog_plugin_id = str(catalog_plugin_id).strip()

    def load(
        self,
        root: Path,
        *,
        origin: CapabilityOrigin = CapabilityOrigin.STANDALONE,
        plugin_id: str | None = None,
    ) -> SkillContribution:
        resolved_root = Path(root).expanduser().resolve()
        manifest_path = resolved_root / "SKILL.md"
        if not manifest_path.is_file():
            raise ValueError(f"SKILL.md is required: {manifest_path}")
        origin = CapabilityOrigin(origin)
        normalized_plugin_id = str(plugin_id).strip() if plugin_id is not None else None
        if origin is CapabilityOrigin.PLUGIN and not normalized_plugin_id:
            raise ValueError("plugin_id is required for plugin-owned skill")
        metadata, files = self.inspect(resolved_root)
        instructions = _parse_skill(manifest_path.read_text(encoding="utf-8"))[1]
        resources = {
            path.relative_to(resolved_root).as_posix(): str(path.resolve())
            for path in files
            if path != manifest_path
        }
        owner = (
            normalized_plugin_id
            if origin is CapabilityOrigin.PLUGIN
            else self._catalog_plugin_id
        )
        return SkillContribution(
            plugin_id=owner or self._catalog_plugin_id,
            spec=SkillSpec(
                name=metadata["name"],
                display_name=metadata.get("display_name", metadata["name"]),
                description=metadata.get("description", ""),
                name_zh=metadata.get("name_zh", ""),
                name_en=metadata.get("name_en", ""),
                description_zh=metadata.get("description_zh", ""),
                version=metadata.get("version", "0.0.0"),
                instructions=instructions,
                resources=resources,
                metadata={
                    "origin": origin.value,
                    "plugin_id": normalized_plugin_id,
                    "source_path": str(manifest_path.resolve()),
                    "manifest_path": str(manifest_path.resolve()),
                    "version": metadata.get("version", "0.0.0"),
                    "name_zh": metadata.get("name_zh", ""),
                    "name_en": metadata.get("name_en", ""),
                    "description_zh": metadata.get("description_zh", ""),
                    "content_digest": digest_directory(resolved_root, files),
                },
            ),
        )

    @staticmethod
    def inspect(root: Path) -> tuple[dict[str, str], tuple[Path, ...]]:
        """Parse a Skill manifest and validate its files without executing code."""

        resolved_root = Path(root).expanduser().resolve()
        manifest_path = resolved_root / "SKILL.md"
        if not manifest_path.is_file():
            raise ValueError(f"SKILL.md is required: {manifest_path}")
        metadata, _ = _parse_skill(manifest_path.read_text(encoding="utf-8"))
        files = installed_content_files(resolved_root)
        return metadata, files


__all__ = ["DirectorySkillLoader"]
