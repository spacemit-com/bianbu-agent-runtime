from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*")
_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_PLUGIN_ID = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
DEFAULT_ACCOUNT_ID = "default"


def _text(value: object, field_name: str, *, required: bool = False) -> str:
    normalized = str(value).strip()
    if required and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


def _plugin(value: object) -> str:
    normalized = _text(value, "plugin_id", required=True)
    if _PLUGIN_ID.fullmatch(normalized) is None:
        raise ValueError("plugin_id must be a stable lowercase identifier")
    return normalized


class HookPoint(str, Enum):
    BEFORE_RUN = "before_run"
    AFTER_CONTEXT = "after_context"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    AFTER_RUN = "after_run"
    ON_ERROR = "on_error"


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: str
    session_id: str
    agent_id: str
    request: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    response: str = ""
    status: str = ""
    account_id: str = DEFAULT_ACCOUNT_ID

    def __post_init__(self) -> None:
        for name in ("run_id", "session_id", "agent_id", "account_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name, required=True))
        object.__setattr__(self, "request", str(self.request))
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        object.__setattr__(self, "response", str(self.response))
        object.__setattr__(self, "status", str(self.status).strip())


@dataclass(frozen=True, slots=True)
class ContextPatch:
    context_sections: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    direct_reply: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "context_sections",
            tuple(str(item) for item in self.context_sections if str(item)),
        )
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        if self.direct_reply is None:
            normalized_reply = None
        else:
            normalized_reply = str(self.direct_reply) or None
        object.__setattr__(self, "direct_reply", normalized_reply)


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    run_id: str
    session_id: str
    agent_id: str
    workspace_ref: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    account_id: str = DEFAULT_ACCOUNT_ID
    progress: Callable[[Mapping[str, object]], None] | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "session_id", "agent_id", "account_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name, required=True))
        object.__setattr__(self, "workspace_ref", str(self.workspace_ref).strip())
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        if self.progress is not None and not callable(self.progress):
            raise ValueError("tool progress sink must be callable")


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    artifacts: tuple[Mapping[str, object], ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", str(self.content))
        object.__setattr__(self, "artifacts", tuple(_mapping(item) for item in self.artifacts))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, object]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = _text(self.name, "tool name", required=True)
        if _TOOL_NAME.fullmatch(normalized) is None:
            raise ValueError("tool name must match [a-z][a-z0-9_]*")
        object.__setattr__(self, "name", normalized)
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "input_schema", _mapping(self.input_schema))
        object.__setattr__(self, "metadata", _mapping(self.metadata))


ToolHandler = Callable[
    [ToolCallContext, Mapping[str, object]],
    Awaitable[ToolResult],
]

@dataclass(frozen=True, slots=True)
class ToolContribution:
    plugin_id: str
    spec: ToolSpec
    handler: ToolHandler

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _plugin(self.plugin_id))
        if not callable(self.handler):
            raise TypeError("tool handler must be callable")


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    description: str = ""
    instructions: str = ""
    resources: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    display_name: str = ""
    #: Optional display names from the Power format (``schema_version: 2``).
    #: They are display-only: ``name`` stays the call name every selector and
    #: run uses, and a missing translation falls back to it.
    name_zh: str = ""
    name_en: str = ""
    description_zh: str = ""
    version: str = "0.0.0"

    def __post_init__(self) -> None:
        normalized = _text(self.name, "skill name", required=True)
        if _SKILL_NAME.fullmatch(normalized) is None:
            raise ValueError("skill name contains unsupported characters")
        object.__setattr__(self, "name", normalized)
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "instructions", _text(self.instructions, "instructions"))
        object.__setattr__(
            self,
            "display_name",
            _text(self.display_name, "display_name") or normalized,
        )
        for field_name in ("name_zh", "name_en", "description_zh"):
            object.__setattr__(
                self, field_name, _text(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self,
            "version",
            _text(self.version, "version", required=True),
        )
        object.__setattr__(
            self,
            "resources",
            MappingProxyType({str(key): str(value) for key, value in self.resources.items()}),
        )
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class SkillContribution:
    plugin_id: str
    spec: SkillSpec

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _plugin(self.plugin_id))


def skill_unavailable_reason(contribution: SkillContribution) -> str:
    """Return the shared availability gate for catalog and Run selection."""

    return str(
        contribution.spec.metadata.get("unavailable_reason") or ""
    ).strip()


@dataclass(frozen=True, slots=True)
class MCPRuntimeContribution:
    """Describe one MCP server the host starts, without a Plugin Console.

    MCP runtimes used to publish this description through the Plugin Console
    contribution.  The Console mechanism is gone, so the host reads the
    server's identity and Tool views from here instead.
    """

    plugin_id: str
    mcp_id: str
    runtime_id: str
    display_name: str
    transport: str
    command: str
    owner_plugin_id: str | None = None
    source_kind: str = "standalone"
    source_path: str = ""
    tools: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _plugin(self.plugin_id))
        object.__setattr__(self, "mcp_id", _text(self.mcp_id, "mcp_id", required=True))
        object.__setattr__(
            self, "runtime_id", _text(self.runtime_id, "runtime_id", required=True)
        )
        object.__setattr__(
            self,
            "display_name",
            _text(self.display_name, "display_name", required=True),
        )
        object.__setattr__(self, "transport", _text(self.transport, "transport"))
        object.__setattr__(self, "command", _text(self.command, "command"))
        owner = self.owner_plugin_id
        object.__setattr__(self, "owner_plugin_id", _plugin(owner) if owner else None)
        object.__setattr__(
            self,
            "source_kind",
            _text(self.source_kind, "source_kind") or "standalone",
        )
        object.__setattr__(self, "source_path", _text(self.source_path, "source_path"))
        object.__setattr__(self, "tools", tuple(_mapping(item) for item in self.tools))


HookHandler = Callable[[RunContext], Awaitable[ContextPatch | None]]


@dataclass(frozen=True, slots=True)
class HookContribution:
    plugin_id: str
    point: HookPoint
    handler: HookHandler
    priority: int = 100
    timeout_seconds: float = 5.0
    id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    # Hooks that declare themselves order-independent may overlap with the
    # other parallel Hooks of the same point.  The merged patch keeps the
    # (priority, hook id) order either way, so opting in changes latency only.
    parallel: bool = False
    # Hook ids (``<plugin>/<hook>``, globbed) that must finish before this one
    # starts.  Only Hooks that sort earlier can be waited for; everything else
    # still overlaps.  A device-side Hook needs this to run after the Security
    # decision without also queueing behind best-effort Hooks.
    waits_for: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _plugin(self.plugin_id))
        object.__setattr__(self, "point", HookPoint(self.point))
        object.__setattr__(
            self,
            "id",
            _plugin(self.plugin_id if self.id is None else self.id),
        )
        if not callable(self.handler):
            raise TypeError("hook handler must be callable")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(self.parallel, bool):
            raise TypeError("parallel must be a boolean")
        waits_for = tuple(self.waits_for)
        if waits_for and not self.parallel:
            raise ValueError("waits_for requires parallel=True")
        for pattern in waits_for:
            if not isinstance(pattern, str) or not pattern or len(pattern) > 512:
                raise ValueError("waits_for entries must be nonempty globs")
        object.__setattr__(self, "waits_for", waits_for)
        object.__setattr__(self, "metadata", _mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class PluginContributions:
    hooks: tuple[HookContribution, ...] = ()
    tools: tuple[ToolContribution, ...] = ()
    skills: tuple[SkillContribution, ...] = ()
    mcp_runtime: MCPRuntimeContribution | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "hooks",
            "tools",
            "skills",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        if self.mcp_runtime is not None and not isinstance(
            self.mcp_runtime, MCPRuntimeContribution
        ):
            raise TypeError("mcp_runtime must be an MCPRuntimeContribution")


__all__ = [
    "ContextPatch",
    "DEFAULT_ACCOUNT_ID",
    "HookContribution",
    "HookHandler",
    "HookPoint",
    "MCPRuntimeContribution",
    "PluginContributions",
    "RunContext",
    "SkillContribution",
    "SkillSpec",
    "ToolCallContext",
    "ToolContribution",
    "ToolHandler",
    "ToolResult",
    "ToolSpec",
]
