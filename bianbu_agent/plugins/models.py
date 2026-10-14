from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from bianbu_agent.plugins.scope import PluginScope


_PLUGIN_ID = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_PLUGIN_MEMBER_KINDS = frozenset({"tool", "skill", "hook", "mcp"})


def _required_text(value: object, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def failure_message(error: object) -> str:
    """Diagnostic text for a failure that may carry no text of its own.

    Transport errors stringify to nothing: ``httpx.ConnectError`` from a
    refused connection and anyio's ``BrokenResourceError`` from a reset stream
    both print as the empty string.  A diagnostic *about* a failure must never
    become the failure itself, so fall back to the exception type, which is the
    only information left, and to a generic phrase for non-exceptions.
    """

    text = str(error).strip()
    if text:
        return text
    if isinstance(error, BaseException):
        return type(error).__name__
    return "plugin failed"


def _plugin_id(value: object, name: str = "id") -> str:
    normalized = _required_text(value, name)
    if _PLUGIN_ID.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a stable lowercase identifier")
    return normalized


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_thaw(item) for item in value]
    return value


class PluginState(str, Enum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    SETUP = "setup"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class PluginContributionManifest:
    """Contributions discovered in one Plugin package directory layout."""

    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    mcps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        all_names: list[str] = []
        for field_name in ("tools", "skills", "hooks", "mcps"):
            raw = getattr(self, field_name)
            if isinstance(raw, str):
                raise ValueError(f"contributions.{field_name} must be a list")
            names = tuple(
                _safe_component(item, f"contributions.{field_name}") for item in raw
            )
            object.__setattr__(self, field_name, names)
            all_names.extend(names)
        duplicates = sorted({name for name in all_names if all_names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate contribution name: {duplicates[0]}")


def _safe_component(value: object, name: str) -> str:
    normalized = str(value).strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
    ):
        raise ValueError(f"{name} must contain safe directory components")
    return normalized

@dataclass(frozen=True, slots=True)
class PluginMember:
    """One validated capability that remains inside its Plugin namespace."""

    namespace: str
    kind: str
    local_name: str
    version: str
    inode: tuple[int, int]
    kind_inode: tuple[int, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespace", _plugin_id(self.namespace, "namespace"))
        kind = _required_text(self.kind, "plugin member kind")
        if kind not in _PLUGIN_MEMBER_KINDS:
            raise ValueError("plugin member kind must be tool, skill, hook, or mcp")
        local_name = _required_text(self.local_name, "plugin member local name")
        if local_name in {".", ".."} or any(char in local_name for char in "/\\\x00"):
            raise ValueError("plugin member local name must be one safe directory component")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "local_name", local_name)
        object.__setattr__(self, "version", _required_text(self.version, "version"))
        inode = tuple(self.inode)
        if len(inode) != 2 or not all(isinstance(item, int) for item in inode):
            raise ValueError("plugin member inode must contain device and inode numbers")
        object.__setattr__(self, "inode", inode)
        kind_inode = tuple(self.kind_inode)
        if len(kind_inode) != 2 or not all(isinstance(item, int) for item in kind_inode):
            raise ValueError("plugin member kind inode must contain device and inode numbers")
        object.__setattr__(self, "kind_inode", kind_inode)

    @property
    def qualified_id(self) -> str:
        return f"{self.namespace}/{self.local_name}"


@dataclass(frozen=True, slots=True)
class PluginPackageIssue:
    """One installed Plugin directory the catalog could not read."""

    path: Path
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    """A published Plugin directory and its namespaced members."""

    manifest: PluginManifest
    content_digest: str
    members: tuple[PluginMember, ...]
    inode: tuple[int, int]

    def __post_init__(self) -> None:
        members = tuple(self.members)
        if any(member.namespace != self.manifest.id for member in members):
            raise ValueError("plugin member namespace must match its Plugin")
        keys = [member.local_name for member in members]
        if len(keys) != len(set(keys)):
            raise ValueError("plugin members must be unique by local name")
        object.__setattr__(self, "members", tuple(sorted(members, key=lambda item: item.qualified_id)))
        inode = tuple(self.inode)
        if len(inode) != 2 or not all(isinstance(item, int) for item in inode):
            raise ValueError("Plugin inode must contain device and inode numbers")
        object.__setattr__(self, "inode", inode)

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def member_ids(self) -> tuple[str, ...]:
        return tuple(member.qualified_id for member in self.members)


@dataclass(frozen=True, slots=True)
class PluginHealth:
    status: str
    message: str = ""

    @classmethod
    def healthy(cls) -> PluginHealth:
        return cls(status="healthy")

    @classmethod
    def degraded(cls, message: str) -> PluginHealth:
        return cls(status="degraded", message=str(message).strip())

    @classmethod
    def unhealthy(cls, message: str) -> PluginHealth:
        return cls(status="unhealthy", message=str(message).strip())


@dataclass(frozen=True, slots=True)
class PluginDiagnostic:
    code: str
    message: str
    plugin_id: str = ""
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required_text(self.code, "code"))
        # A diagnostic about a failed Plugin must stay constructible: reporting
        # the failure may not raise.  An empty message falls back to the code,
        # which every caller sets to the failure it observed; the structured
        # text for humans comes from ``failure_message`` at the call site.
        object.__setattr__(
            self, "message", str(self.message).strip() or _required_text(self.code, "code")
        )
        if self.plugin_id:
            object.__setattr__(self, "plugin_id", _plugin_id(self.plugin_id, "plugin_id"))
        object.__setattr__(self, "details", _freeze(self.details))


@dataclass(frozen=True, slots=True)
class PluginDependency:
    plugin_id: str
    version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _plugin_id(self.plugin_id, "plugin_id"))
        normalized = str(self.version).strip()
        if normalized:
            try:
                SpecifierSet(normalized)
            except InvalidSpecifier as exc:
                raise ValueError(f"version must be a valid PEP 440 specifier: {normalized}") from exc
        object.__setattr__(self, "version", normalized)

    def accepts(self, version: str) -> bool:
        return not self.version or Version(str(version)) in SpecifierSet(self.version)


class PluginConfigError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        path: tuple[object, ...] = (),
    ) -> None:
        self.path = path
        location = ".".join(str(part) for part in path) or "<root>"
        super().__init__(f"invalid plugin config at {location}: {message}")


@dataclass(frozen=True, slots=True)
class PluginManifest:
    id: str
    version: str
    description: str = ""
    dependencies: tuple[PluginDependency, ...] = ()
    config_schema: Mapping[str, object] = field(default_factory=dict)
    contributions: PluginContributionManifest = field(
        default_factory=PluginContributionManifest
    )
    entrypoint: str = ""
    config_defaults: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _plugin_id(self.id))
        normalized_version = _required_text(self.version, "version")
        try:
            Version(normalized_version)
        except InvalidVersion as exc:
            raise ValueError(f"version must be a valid PEP 440 version: {normalized_version}") from exc
        object.__setattr__(self, "version", normalized_version)
        object.__setattr__(self, "description", str(self.description).strip())
        dependencies = tuple(self.dependencies)
        dependency_ids = [item.plugin_id for item in dependencies]
        if self.id in dependency_ids:
            raise ValueError(f"plugin {self.id} must not depend on itself")
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ValueError(f"plugin {self.id} has duplicate dependency IDs")
        object.__setattr__(self, "dependencies", dependencies)
        schema = _thaw(self.config_schema)
        if schema != {}:
            from jsonschema.validators import validator_for

            validator_type = validator_for(schema)
            try:
                validator_type.check_schema(schema)
            except Exception as exc:
                raise ValueError(f"config_schema is not a valid JSON Schema: {exc}") from exc
        object.__setattr__(self, "config_schema", _freeze(schema))
        object.__setattr__(self, "config_defaults", _freeze(dict(self.config_defaults)))
        if not isinstance(self.contributions, PluginContributionManifest):
            raise TypeError("contributions must be a PluginContributionManifest")
        entrypoint = str(self.entrypoint).strip()
        if entrypoint:
            path, separator, callable_name = entrypoint.partition(":")
            if (
                not separator
                or not callable_name.isidentifier()
                or Path(path).name != path
                or not path.endswith(".py")
            ):
                raise ValueError("entrypoint must be <root-python-file>:<callable>")
        object.__setattr__(self, "entrypoint", entrypoint)

    def validate_config(
        self,
        config: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        prepared = _thaw(config or {})
        schema = _thaw(self.config_schema)
        if schema == {}:
            return _freeze(prepared)
        from jsonschema.validators import validator_for

        validator = validator_for(schema)(schema)
        errors = sorted(validator.iter_errors(prepared), key=lambda item: list(item.path))
        if errors:
            error = errors[0]
            raise PluginConfigError(error.message, path=tuple(error.path))
        return _freeze(prepared)


@dataclass(frozen=True, slots=True)
class PluginContext:
    plugin_id: str
    data_dir: Path
    config: Mapping[str, object] = field(default_factory=dict)
    scope: PluginScope | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _plugin_id(self.plugin_id, "plugin_id"))
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "config", _freeze(self.config))
        if self.scope is None:
            from bianbu_agent.plugins.scope import PluginScope

            object.__setattr__(self, "scope", PluginScope(self.plugin_id))

    def require(self, name: str) -> object:
        assert self.scope is not None
        return self.scope.require(name)


class Plugin(Protocol):
    manifest: PluginManifest

    async def setup(self, context: PluginContext) -> None: ...

    async def start(self) -> None: ...

    async def health(self) -> PluginHealth: ...

    async def reconfigure(self, config: Mapping[str, object]) -> None: ...

    async def stop(self) -> None: ...


PluginFactory = Callable[[PluginContext], Plugin | Awaitable[Plugin]]


# The install and runtime paths intentionally share one manifest type.
__all__ = [
    "InstalledPlugin",
    "Plugin",
    "PluginConfigError",
    "PluginContributionManifest",
    "PluginContext",
    "PluginDependency",
    "PluginDiagnostic",
    "PluginFactory",
    "PluginHealth",
    "PluginManifest",
    "PluginMember",
    "PluginState",
]
