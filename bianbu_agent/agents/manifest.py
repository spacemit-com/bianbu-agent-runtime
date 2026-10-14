"""Strict YAML Manifest model for directory-backed Agent packages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any

import yaml

from bianbu_agent._internal.yaml_documents import StrictSafeLoader
from bianbu_agent.agents import AgentModel, AgentSpec


_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_VERSION = re.compile(
    r"v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
)
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "agent"})
_AGENT_FIELDS = frozenset(
    {
        "id",
        "version",
        "name",
        "name_zh",
        "name_en",
        "description",
        "description_zh",
        "soul",
        "model",
        "tools",
        "skills",
        "mcps",
        "plugins",
        "permissions",
        "metadata",
    }
)
_REQUIRED_AGENT_FIELDS = frozenset({"id", "version", "name"})
# The version this Runtime writes.  1 is still read so a record written by an
# older Runtime keeps working; everything new is written as version 2, the
# format the AgentHub/APM catalog publishes.
_SCHEMA_VERSION = 2


def validate_agent_id(value: object) -> str:
    """Return one safe Agent package ID or raise a stable validation error."""

    if not isinstance(value, str):
        raise ValueError("agent.id must be a string")
    normalized = value.strip()
    if _AGENT_ID.fullmatch(normalized) is None:
        raise ValueError(
            "agent.id must start with a letter or digit, contain only letters, "
            "digits, '.', '_' or '-', and be at most 128 characters"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class AgentManifest:
    """Versioned, framework-neutral declaration stored in ``agent.yaml``."""

    schema_version: int
    version: str
    spec: AgentSpec

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in (1, 2):
            raise ValueError("agent manifest schema_version must be 1 or 2")
        if not isinstance(self.version, str) or _VERSION.fullmatch(self.version) is None:
            raise ValueError("agent.version must use major.minor.patch with optional v prefix")
        validate_agent_id(self.spec.id)
        _validate_selectors(self.spec.tools, "agent.tools")
        _validate_selectors(self.spec.skills, "agent.skills")
        _validate_selectors(self.spec.mcps, "agent.mcps")
        _validate_selectors(self.spec.plugins, "agent.plugins")
        _thaw_permissions(self.spec.permissions)
        _thaw_metadata(self.spec.metadata)

    @classmethod
    def from_path(cls, path: Path) -> AgentManifest:
        manifest_path = Path(path)
        try:
            payload = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read agent manifest: {manifest_path}") from exc
        try:
            return cls.from_yaml(payload)
        except ValueError as exc:
            raise ValueError(f"invalid agent manifest {manifest_path}: {exc}") from exc

    @classmethod
    def from_yaml(cls, value: str) -> AgentManifest:
        if not isinstance(value, str):
            raise ValueError("agent manifest must be YAML text")
        try:
            payload = yaml.load(value, Loader=StrictSafeLoader)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid agent YAML: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("agent manifest root must be a mapping")
        unknown_top = set(payload) - _TOP_LEVEL_FIELDS
        if unknown_top:
            raise ValueError("agent.yaml contains unsupported top-level fields")
        if set(payload) != _TOP_LEVEL_FIELDS:
            missing = sorted(_TOP_LEVEL_FIELDS - set(payload))
            raise ValueError("agent.yaml is missing " + ", ".join(missing))
        if type(payload["schema_version"]) is not int:
            raise ValueError("schema_version must be an integer")
        agent = payload["agent"]
        if not isinstance(agent, Mapping):
            raise ValueError("agent must be a mapping")
        agent = dict(agent)
        unknown_agent = set(agent) - _AGENT_FIELDS
        if unknown_agent:
            raise ValueError("agent.yaml contains unsupported agent fields")
        missing_agent = sorted(_REQUIRED_AGENT_FIELDS - set(agent))
        if missing_agent:
            raise ValueError("agent.yaml is missing agent." + ", agent.".join(missing_agent))
        for field_name in (
            "id",
            "version",
            "name",
            "name_zh",
            "name_en",
            "description",
            "description_zh",
            "soul",
        ):
            if field_name in agent and not isinstance(agent[field_name], str):
                raise ValueError(f"agent.{field_name} must be a string")
        tools = _parse_selectors(agent.get("tools", []), "agent.tools")
        skills = _parse_selectors(agent.get("skills", []), "agent.skills")
        mcps = _parse_selectors(agent.get("mcps", []), "agent.mcps")
        plugins = _parse_selectors(agent.get("plugins", []), "agent.plugins")
        metadata = agent.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("agent.metadata must be a mapping")
        metadata = _thaw_metadata(metadata)
        permissions = _thaw_permissions(agent.get("permissions", {}))
        model_value = agent.get("model")
        if isinstance(model_value, Mapping):
            if set(model_value) != {"provider", "name"}:
                raise ValueError("agent.model must contain provider and name")
            model: AgentModel | None = AgentModel(
                provider=model_value["provider"],
                name=model_value["name"],
            )
        elif isinstance(model_value, str):
            raise ValueError("agent.model must be a mapping or null")
        elif model_value is None:
            model = None
        else:
            raise ValueError("agent.model must be a mapping or null")
        return cls(
            schema_version=payload["schema_version"],
            version=agent["version"],
            spec=AgentSpec(
                id=validate_agent_id(agent["id"]),
                name=agent["name"],
                name_zh=agent.get("name_zh", ""),
                name_en=agent.get("name_en", ""),
                description=agent.get("description", ""),
                description_zh=agent.get("description_zh", ""),
                soul=agent.get("soul", ""),
                model=model,
                tools=tools,
                skills=skills,
                mcps=mcps,
                plugins=plugins,
                permissions=permissions,
                metadata=metadata,
            ),
        )

    def to_yaml(self) -> str:
        spec = self.spec
        return yaml.safe_dump(
            {
                "schema_version": _SCHEMA_VERSION,
                "agent": {
                    "id": spec.id,
                    "version": self.version,
                    "name": spec.name,
                    "name_zh": spec.name_zh,
                    "name_en": spec.name_en,
                    "description": spec.description,
                    "description_zh": spec.description_zh,
                    "soul": spec.soul,
                    "model": (
                        {
                            "provider": spec.model.provider,
                            "name": spec.model.name,
                        }
                        if isinstance(spec.model, AgentModel)
                        else None
                    ),
                    "tools": list(spec.tools),
                    "skills": list(spec.skills),
                    "mcps": list(spec.mcps),
                    "plugins": list(spec.plugins),
                    "permissions": _thaw_permissions(spec.permissions),
                    "metadata": _thaw_metadata(spec.metadata),
                },
            },
            allow_unicode=True,
            sort_keys=False,
        )


def _parse_selectors(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be an array of strings")
    selectors = tuple(value)
    _validate_selectors(selectors, field_name)
    return selectors


def _validate_selectors(selectors: Sequence[str], field_name: str) -> None:
    if any(not item.strip() or item != item.strip() for item in selectors):
        raise ValueError(f"{field_name} entries must be non-empty trimmed strings")
    if len(set(selectors)) != len(selectors):
        raise ValueError(f"{field_name} contains duplicate entries")
    if "*" in selectors and tuple(selectors) != ("*",):
        raise ValueError(f"{field_name} wildcard must be the only entry")


def _thaw_metadata(value: object) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("agent.metadata keys must be strings")
            result[key] = _thaw_metadata(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_thaw_metadata(item) for item in value]
    if isinstance(value, bool) or isinstance(value, (str, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("agent.metadata contains a value unsupported by YAML")


def _thaw_permissions(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("agent.permissions must be a mapping")
    try:
        thawed = _thaw_metadata(value)
    except ValueError as exc:
        raise ValueError(
            "agent.permissions contains a value unsupported by YAML"
        ) from exc
    return thawed


__all__ = ["AgentManifest", "validate_agent_id"]
