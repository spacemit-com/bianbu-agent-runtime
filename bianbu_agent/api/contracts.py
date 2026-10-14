"""Strict request and response models for the versioned HTTP API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)


# Arbitrary JSON-shaped values are permitted inside declared metadata and options
# objects.  The enclosing request models remain strict about their own fields.
JSONValue = Any


def _validate_agent_metadata(value: dict[str, JSONValue]) -> dict[str, JSONValue]:
    """Reject Agent-declared execution modes.

    Whether an Agent runs in a sandbox follows from the security settings it
    declares in the security console; securityd derives it and the security
    API is the only writer. An Agent declaration cannot name a side, so any
    ``requested_mode`` in metadata is refused instead of silently ignored.
    """
    for section in ("runtime", "execution"):
        block = value.get(section)
        if isinstance(block, dict) and block.get("requested_mode") is not None:
            raise ValueError(
                f"metadata.{section}.requested_mode is retired; the execution boundary "
                "is derived from the Agent's security settings and configured in the "
                "security console"
            )
    return value


class StrictModel(BaseModel):
    """Reject request keys that the public API has not declared."""

    model_config = ConfigDict(extra="forbid")


class SessionPromptRequest(StrictModel):
    message_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    content: str = Field(min_length=1, max_length=16_000)
    attachments: list[dict[str, JSONValue]] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("skill_ids")
    @classmethod
    def normalize_skill_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(value).strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("skill_ids must not contain empty values")
        return normalized


class SessionTitleGenerateRequest(StrictModel):
    message_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    content: str = Field(min_length=1, max_length=16_000)


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalDecisionRequest(StrictModel):
    decision: ApprovalDecision
    reason: str = ""


class SessionCreateRequest(StrictModel):
    id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    agent_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    title: str = ""
    metadata: dict[str, JSONValue] = Field(default_factory=dict)


class SessionUpdateRequest(StrictModel):
    title: str | None = None


class AgentModelRequest(StrictModel):
    provider: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)


class AgentCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    name_zh: str = Field(default="", max_length=256)
    name_en: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=4_096)
    description_zh: str = Field(default="", max_length=4_096)
    soul: str = Field(default="", max_length=100_000)
    model: AgentModelRequest | None = None
    tools: list[str] = Field(default_factory=list, max_length=1_000)
    skills: list[str] = Field(default_factory=list, max_length=1_000)
    mcps: list[str] = Field(default_factory=list, max_length=1_000)
    plugins: list[str] = Field(default_factory=list, max_length=1_000)
    permissions: dict[str, JSONValue] = Field(default_factory=dict)
    metadata: dict[str, JSONValue] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, JSONValue]) -> dict[str, JSONValue]:
        return _validate_agent_metadata(value)


class AgentUpdateRequest(StrictModel):
    name: str | None = None
    name_zh: str | None = None
    name_en: str | None = None
    description: str | None = None
    description_zh: str | None = None
    soul: str | None = None
    model: AgentModelRequest | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    mcps: list[str] | None = None
    plugins: list[str] | None = None
    permissions: dict[str, JSONValue] | None = None
    metadata: dict[str, JSONValue] | None = None

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, JSONValue] | None) -> dict[str, JSONValue] | None:
        return None if value is None else _validate_agent_metadata(value)

class MCPServerInstallRequest(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    command: str = Field(min_length=1, max_length=8_192)
    upgrade: bool = False


class MCPServerToolView(StrictModel):
    id: str
    name: str
    display_name: str


class MCPServerView(StrictModel):
    id: str
    display_name: str
    command: str
    kind: Literal["mcp"]
    #: The raw status of the layer that reported it, kept for compatibility.
    #: New consumers read ``state`` instead: "preparing" and "not_loaded" never
    #: told an operator what was actually happening.
    status: str
    #: Lifecycle shared by every surface: not_loaded | preparing | ready |
    #: degraded | failed | stopped | unknown.
    state: str = "unknown"
    #: While ``state`` is ``preparing``: installing | starting. Empty otherwise.
    phase: str = ""
    #: Whether the server answered and its tools are callable.
    ready: bool = False
    #: How many tools the server published; 0 while it never answered.
    tools_count: int = 0
    transport: str
    tools: list[MCPServerToolView]
    source_kind: str
    owner_plugin_id: str | None
    removable: bool
    health_checked_at: datetime | None
    #: Why this server is not healthy, in one line an operator can act on.
    #: Empty while the server answers; a Plugins page cannot explain a missing
    #: tool set without it.
    detail: str = ""


class MCPServerCollectionEnvelope(StrictModel):
    data: list[MCPServerView]


class ProviderCreateRequest(StrictModel):
    id: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    parameters: dict[str, JSONValue] = Field(default_factory=dict)


class ProviderUpdateRequest(StrictModel):
    label: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    parameters: dict[str, JSONValue] | None = None


class ProviderView(StrictModel):
    id: str
    label: str
    base_url: str
    configured: bool
    api_key_masked: str
    # ``system`` means the package ships this record; ``user`` means it was
    # created on the device.  Clients need it because ``record_kind`` is
    # ``custom_provider`` for packaged and hand-made records alike.
    origin: str
    parameters: dict[str, JSONValue] = Field(default_factory=dict)


EffectiveCapabilityKind = Literal["tool", "skill", "hook"]


class CapabilityView(StrictModel):
    id: str
    name: str
    display_name: str
    description: str
    #: Optional display metadata from the Power format (``schema_version: 2``).
    #: A client shows ``name_zh``/``name_en`` for its own language and falls
    #: back to ``name`` when the field is empty, so Skills written before v2
    #: keep their current label.
    name_zh: str = ""
    name_en: str = ""
    description_zh: str = ""
    #: Short UI intro per language, for Runtime-owned tools only; tools from
    #: other owners (user, Plugin, MCP) have none and their own description is
    #: shown instead.
    summaries: dict[str, str] = Field(default_factory=dict)
    version: str
    origin: str
    plugin_id: str | None = None
    enabled: bool
    assignable: bool = True
    # A preset capability is materialized into every Agent whatever its own
    # selectors say, so an editor must not offer to turn it off.
    preset: bool = False
    unavailable_reason: str


class CapabilityCollectionEnvelope(StrictModel):
    data: list[CapabilityView]


class PackageKind(str, Enum):
    SKILL = "skill"
    AGENT = "agent"


class PackageInstallRequest(StrictModel):
    source: str
    upgrade: bool = False


class PluginCreateRequest(StrictModel):
    source: str
    upgrade: bool = False


class PluginActionRequest(RootModel[dict[str, object]]):
    """The sole dynamic JSON body: a Plugin-defined action object."""


class PaginationQuery(StrictModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)


class SessionEventQuery(StrictModel):
    before_seq: int | None = Field(default=None, ge=0)
    max_messages: int = Field(default=50, ge=1, le=50)
    # Incremental recovery read: everything after this position, unfolded.  The
    # raw window above answers "show me the conversation"; this answers "tell me
    # what I missed", which a reader holding a contiguous cursor cannot express
    # with a backwards page.
    from_seq: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def reject_mixed_read_modes(self) -> SessionEventQuery:
        if self.from_seq is not None and self.before_seq is not None:
            raise ValueError("from_seq and before_seq are mutually exclusive")
        return self


class SessionSearchQuery(StrictModel):
    """Substring query over conversation text.  An empty query is not a listing."""

    q: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator("q")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        # 纯空白查询语义上等于「列出全部」，白扫一遍全部转录还可能返回任意前 20 条。
        stripped = value.strip()
        if not stripped:
            raise ValueError("q must contain at least one non-whitespace character")
        return stripped


class DeploymentSettingsRequest(StrictModel):
    language: Literal["zh", "en"] | None = None


class RuntimeSettingsRequest(StrictModel):
    search_strategy: Literal["searxng"] | None = None
    self_evolution_enabled: bool | None = None
    self_evolution_idle_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    self_evolution_min_turns: int | None = Field(default=None, ge=1, le=1_000)


class SettingsUpdateRequest(StrictModel):
    deployment: DeploymentSettingsRequest | None = None
    runtime: RuntimeSettingsRequest | None = None

    @model_validator(mode="after")
    def reject_mixed_store_updates(self) -> SettingsUpdateRequest:
        """Match the initial non-atomic settings-store update semantics."""

        if self.deployment is None and self.runtime is None:
            raise ValueError("one of deployment or runtime is required")
        if self.deployment is not None and self.runtime is not None:
            raise ValueError("deployment and runtime settings must be updated separately")
        return self


class ErrorView(StrictModel):
    code: str
    message: str
    details: dict[str, JSONValue] = Field(default_factory=dict)


class ErrorEnvelope(StrictModel):
    error: ErrorView


class SuccessEnvelope(StrictModel):
    data: JSONValue


class RuntimeStatusView(StrictModel):
    """Non-secret control-plane and managed Worker readiness view."""

    status: Literal["CONTROL_READY", "AGENT_READY", "DEGRADED", "STARTING", "STOPPING"]
    role: Literal["control", "worker"]
    control_ready: bool
    agent_ready: bool
    # A device has one system-level Runtime, so callers have to be able to ask
    # whose account and data directory this process serves instead of guessing
    # that ``~/.bianbu-agent`` belongs to them.
    service_user: str = ""
    data_dir: str = ""
    instance_id: str | None = None
    pid: int | None = None
    effective_mode: Literal["host", "sandbox"] | None = None
    backend: str | None = None
    sandboxed: bool = False
    generation_id: int | None = None
    worker_state: str = "stopped"
    security_pending: dict[str, JSONValue] = Field(default_factory=dict)
    seccomp: dict[str, JSONValue] = Field(default_factory=dict)
    audit: dict[str, JSONValue] = Field(default_factory=dict)


class RuntimeStatusEnvelope(StrictModel):
    data: RuntimeStatusView


class RuntimeWorkerStatusView(StrictModel):
    agent_id: str
    state: str
    instance_id: str | None = None
    pid: int | None = None
    effective_mode: Literal["host", "sandbox"] | None = None
    backend: str | None = None
    sandboxed: bool = False
    generation_id: int | None = None
    catalog_stale: bool = False
    memory_registration: dict[str, JSONValue] | None = None
    error: str | None = None
    seccomp: dict[str, JSONValue] = Field(default_factory=dict)
    audit: dict[str, JSONValue] = Field(default_factory=dict)


class RuntimeControlStatusView(StrictModel):
    pid: int | None = None
    state: str
    control_ready: bool
    role: Literal["control"] = "control"
    effective_mode: Literal["host", "sandbox"] | None = None
    backend: str | None = None
    sandboxed: bool = False


class RuntimeWorkersView(StrictModel):
    runtime: RuntimeStatusView
    control: RuntimeControlStatusView
    worker_count: int = Field(ge=0)
    workers: list[RuntimeWorkerStatusView] = Field(default_factory=list)


class RuntimeWorkersEnvelope(StrictModel):
    data: RuntimeWorkersView


class OperationalLogView(StrictModel):
    timestamp: float
    level: str
    logger: str
    message: str
    agent_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    plugin_generation: int | None = None
    provider_request_id: str | None = None
    event: str | None = None
    run_status: str | None = None
    error_code: str | None = None
    duration_ms: int | float | None = None
    exception: str | None = None


class OperationalLogCollectionEnvelope(StrictModel):
    data: list[OperationalLogView]


class PageView(StrictModel):
    number: int
    size: int
    has_more: bool


class CollectionEnvelope(StrictModel):
    data: list[JSONValue]
    page: PageView


class SessionSearchHit(StrictModel):
    """One conversation-text hit with the window the client pages back to."""

    session_id: str
    title: str
    updated_at: float
    run_id: str | None = None
    seq: int = Field(ge=0)
    #: 命中消息覆盖到的最后一条事件；客户端读 `before_seq=end_seq+1` 才拿到完整消息。
    end_seq: int = Field(ge=0)
    role: Literal["user", "assistant"]
    snippet: str
    match_start: int = Field(ge=0)
    match_length: int = Field(ge=1)


class SessionSearchEnvelope(StrictModel):
    data: list[SessionSearchHit]
    query: str
    limit: int
    has_more: bool


class SessionView(StrictModel):
    id: str
    title: str
    agent_id: str
    metadata: dict[str, JSONValue] = Field(default_factory=dict)
    created_at: float
    updated_at: float


class AgentView(StrictModel):
    id: str
    origin: Literal["system", "user"] = "user"
    name: str
    name_zh: str
    name_en: str = ""
    avatar: str
    description: str
    description_zh: str = ""
    soul: str
    model: AgentModelRequest | None
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcps: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    permissions: dict[str, JSONValue] = Field(default_factory=dict)
    metadata: dict[str, JSONValue] = Field(default_factory=dict)
    execution: dict[str, JSONValue] = Field(default_factory=dict)
    available: bool
    unavailable_reason: str
    # Declared Powers this device has not installed. The Agent still runs and
    # can provision them through APM, so they never make it unavailable.
    missing_powers: list[str] = Field(default_factory=list)


class AgentEnvelope(StrictModel):
    data: AgentView


class AgentCollectionEnvelope(StrictModel):
    data: list[AgentView]


ExecutionMode = Literal["sandbox", "host"]
ExecutionStateValue = Literal[
    "STARTING", "RUNNING", "STOPPING", "STOPPED", "FAILED", "REVOKED"
]


class ExecutionStartRequest(StrictModel):
    """受控 Agent 启动请求。

    entrypoint、cwd、package/config digest 均不属于 HTTP 信任边界；它们由
    Runtime 从已安装 Agent 包读取，并由 security.runtime 返回的 immutable
    policy 决定。额外字段（尤其 command）必须被拒绝。
    """

    # 执行模式不在请求里声明：securityd 根据该 Agent 的安全设置推导 host /
    # sandbox。这里保留 ``auto`` 作为唯一取值，让旧调用方继续能编译。
    requested_mode: Literal["auto"] = "auto"
    env: dict[str, str] = Field(default_factory=dict, max_length=32)
    target_port: int | None = Field(default=None, ge=1, le=65535)
    listen_port: int | None = Field(default=None, ge=0, le=65535)

    @model_validator(mode="after")
    def validate_ports(self) -> ExecutionStartRequest:
        if self.listen_port is not None and self.target_port is None:
            raise ValueError("listen_port requires target_port")
        for key, value in self.env.items():
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError("env contains an invalid variable name or value")
            if key.startswith("BIANBU_WORKER_") or key in {
                "BIANBU_AGENT_ID", "BIANBU_RUNTIME_ROLE", "BIANBU_RUNTIME_MODE",
                "BIANBU_RUNTIME_MANAGED", "BIANBU_WORKER_RPC_TOKEN",
            }:
                raise ValueError("env contains a Runtime-reserved variable")
            if len(key) > 128 or len(value) > 8_192:
                raise ValueError("env variable name or value is too long")
        return self


class ExecutionStopRequest(StrictModel):
    reason: str = Field(default="requested", min_length=1, max_length=1_024)


class ExecutionRevokeRequest(StrictModel):
    reason: str = Field(default="security revoke", min_length=1, max_length=1_024)


class ExecutionView(StrictModel):
    instance_id: str
    agent_id: str
    backend: str
    pid: int = Field(ge=0)
    cgroup_path: str
    unit: str
    unit_scope: Literal["user", "system"] = "user"
    state: ExecutionStateValue
    lease_id: str
    started_at: float
    sandboxed: bool
    container_id: str = ""
    reason: str = ""
    proxy_port: int | None = Field(default=None, ge=1, le=65535)


class ExecutionEnvelope(StrictModel):
    data: ExecutionView


class ExecutionCollectionEnvelope(StrictModel):
    data: list[ExecutionView]


class ExecutionControlView(StrictModel):
    accepted: bool


class ExecutionControlEnvelope(StrictModel):
    data: ExecutionControlView


class AgentTopSummary(StrictModel):
    agents: int = Field(ge=0)
    available_agents: int = Field(ge=0)
    sessions: int = Field(ge=0)
    runs: int = Field(ge=0)
    active_runs: int = Field(ge=0)
    waiting_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)


class AgentTopAgentView(StrictModel):
    id: str
    name: str
    name_zh: str
    name_en: str = ""
    avatar: str
    available: bool
    status: Literal["running", "waiting", "idle", "unavailable"]
    session_count: int = Field(ge=0)
    run_count: int = Field(ge=0)
    active_run_count: int = Field(ge=0)
    failed_run_count: int = Field(ge=0)
    last_active_at: float


class AgentTopRunView(StrictModel):
    id: str
    session_id: str
    session_title: str
    agent_id: str
    status: Literal["queued", "running", "waiting", "completed", "failed", "cancelled"]
    created_at: float
    updated_at: float


class AgentTopView(StrictModel):
    summary: AgentTopSummary
    agents: list[AgentTopAgentView]
    recent_runs: list[AgentTopRunView]
    server_time: float


class AgentTopEnvelope(StrictModel):
    data: AgentTopView


__all__ = [
    "AgentCollectionEnvelope",
    "AgentCreateRequest",
    "AgentEnvelope",
    "AgentModelRequest",
    "AgentUpdateRequest",
    "AgentView",
    "AgentTopAgentView",
    "AgentTopEnvelope",
    "AgentTopRunView",
    "AgentTopSummary",
    "AgentTopView",
    "ExecutionCollectionEnvelope",
    "ExecutionControlEnvelope",
    "ExecutionControlView",
    "ExecutionEnvelope",
    "ExecutionRevokeRequest",
    "ExecutionStartRequest",
    "ExecutionStopRequest",
    "ExecutionView",
    "ExecutionMode",
    "ExecutionStateValue",
    "ApprovalDecision",
    "ApprovalDecisionRequest",
    "CapabilityCollectionEnvelope",
    "CapabilityView",
    "CollectionEnvelope",
    "DeploymentSettingsRequest",
    "ErrorEnvelope",
    "ErrorView",
    "EffectiveCapabilityKind",
    "JSONValue",
    "MCPServerCollectionEnvelope",
    "MCPServerInstallRequest",
    "MCPServerToolView",
    "MCPServerView",
    "PackageInstallRequest",
    "PackageKind",
    "PageView",
    "PaginationQuery",
    "PluginActionRequest",
    "PluginCreateRequest",
    "ProviderCreateRequest",
    "ProviderUpdateRequest",
    "ProviderView",
    "RuntimeSettingsRequest",
    "SessionCreateRequest",
    "SessionEventQuery",
    "SessionPromptRequest",
    "SessionUpdateRequest",
    "SessionView",
    "SettingsUpdateRequest",
    "StrictModel",
    "SuccessEnvelope",
]
