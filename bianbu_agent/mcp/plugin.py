"""Directory Plugin adapter that exposes one MCP server as namespaced Tools."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import shlex
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from bianbu_agent.mcp.client import MCPClient, MCPRemoteTool
from bianbu_agent.mcp.config import (
    MCPServerConfig,
    load_mcp_config,
    validate_mcp_config,
)
from bianbu_agent.mcp.lifecycle import MCP_PHASE_INSTALLING
from bianbu_agent.mcp.preparation import (
    MCP_PREPARATION_FAILED,
    MCP_PREPARING,
    MCPPreparationManager,
)
from bianbu_agent.mcp.readiness import (
    TRANSIENT_RETRY_BACKOFF_SECONDS,
    is_transient_transport_error,
)
from bianbu_agent.plugins.contributions import (
    MCPRuntimeContribution,
    ToolContribution,
    ToolResult,
    ToolSpec,
)
from bianbu_agent.plugins.models import (
    PluginContext,
    PluginHealth,
    PluginManifest,
)

# Execution context attached to every ``tools/call`` request. An MCP child gets
# no session identity from its environment, so the Worker publishes the run it
# is serving through ``_meta``. Servers treat it as a scoping hint, never as an
# authorization input: identity stays with memoryd's peer verification.
HOST_CONTEXT_KEY = "bianbu/host"

# 远端 MCP 是网络服务：一次连接失败不该让这台设备整轮失去该服务器的工具。
# 每次重试都必须新建 client —— MCP 的传输带着 AnyIO cancel scope，失败过的连接
# 不能复用；退避时间与后台探测共用，短于 mcp.json 的 timeout_seconds。
_CONNECT_ATTEMPTS = 3
_CONNECT_BACKOFF_SECONDS = TRANSIENT_RETRY_BACKOFF_SECONDS

logger = logging.getLogger(__name__)


def _host_context(context: object) -> dict[str, str]:
    return {
        name: str(getattr(context, name, "") or "")
        for name in ("session_id", "run_id", "agent_id", "account_id")
    }


def _progress_forwarder(progress, qualified_id: str):
    """把 MCP 的 progress 通知映射成 Runtime 的进度载荷。

    MCP 的 ``total`` 允许缺省（进度只是区间中段），这里不猜分母，缺省就
    不写 total；消息原样透传，由服务端决定是否给出可展示文案。
    """

    async def forward(
        value: float,
        total: float | None,
        message: str | None,
    ) -> None:
        payload: dict[str, object] = {
            "phase": "tool_progress",
            "tool": qualified_id,
            "progress": value,
        }
        if total is not None:
            payload["total"] = total
        if message:
            payload["message"] = str(message)
        progress(payload)

    return forward


def _annotation_metadata(
    annotations: Mapping[str, object],
) -> dict[str, object]:
    """把 MCP 注解降级成 Runtime 审批策略认识的元数据。

    ``destructiveHint`` 是服务端自述的破坏性提示，直接映射成既有的
    ``requires_approval``，让 ``build_langgraph_agent`` 的 ``protected_names``
    能收集到它；只读工具不参与静态保护。服务端没给注解时不产生任何字段，
    行为与接入注解前一致。
    """

    if not annotations:
        return {}
    flags: dict[str, object] = {"mcp_annotations": dict(annotations)}
    read_only = annotations.get("read_only")
    destructive = annotations.get("destructive")
    if isinstance(read_only, bool):
        flags["read_only"] = read_only
    if isinstance(destructive, bool):
        flags["destructive"] = destructive
        flags["requires_approval"] = destructive and read_only is not True
    return flags


ClientFactory = Callable[[MCPServerConfig], MCPClient]
MCPAuthorizer = Callable[[str, str], bool | Awaitable[bool]]
_UNSAFE_NAME = re.compile(r"[^a-z0-9]+")


class MCPDirectoryPlugin:
    def __init__(
        self,
        manifest: PluginManifest,
        *,
        root: Path,
        workspace: Path,
        mcp_id: str | None = None,
        owner_plugin_id: str | None = None,
        source_kind: str = "standalone",
        authorize: MCPAuthorizer | None = None,
        client_factory: ClientFactory = MCPClient,
    ) -> None:
        self.manifest = manifest
        self._root = Path(root).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self._source = (self._root / "mcp.json").resolve()
        self._client_factory = client_factory
        self.mcp_id = str(mcp_id or manifest.id)
        self.runtime_id = manifest.id
        self.owner_plugin_id = owner_plugin_id
        self.source_kind = str(source_kind)
        self._authorize = authorize
        self._config: MCPServerConfig | None = None
        self._public_config: MCPServerConfig | None = None
        self._client: MCPClient | None = None
        #: Set when the Runtime owns background preparation: the first
        #: Generation then registers this server as preparing instead of
        #: blocking on a start that may still be downloading its package.
        self._preparation: MCPPreparationManager | None = None
        self._tool_views: tuple[dict[str, str], ...] = ()
        self._status = "stopped"

    async def setup(self, context: PluginContext) -> None:
        if context.plugin_id != self.manifest.id:
            raise ValueError("MCP Plugin context ID does not match its manifest")
        config = load_mcp_config(self._source, workspace=self._workspace)
        public_config = validate_mcp_config(
            self._source,
            workspace=self._workspace,
        )
        self._config = config
        self._public_config = public_config
        preparations = context.scope.service_view if context.scope is not None else {}
        prepared = preparations.get(MCPPreparationManager.service_name)
        if isinstance(prepared, MCPPreparationManager):
            self._preparation = prepared
            # 这台 Server 马上就会被这次 Generation 连上，所以准备管理器只负责
            # 把它变成可启动（必要时固化 npm 包），不要再起一个后台探测去抢同一
            # 台 Server —— 那份工具清单由这次连接给出，比探测更权威。
            state = prepared.ensure(self.mcp_id, config, source=self._source, probe=False)
            if not state.ready and not state.launchable:
                # Inside a Generation build, waiting for a server that is still
                # downloading its package would stall every other capability.
                # Register the server now and let the preparation reload
                # publish its tools once they exist.
                self._status = state.status
                assert context.scope is not None
                context.scope.register_mcp_runtime(self._mcp_runtime_contribution())
                return None
            # A declaration that names an npm package runs its local
            # installation, which the preparation resolved without npm work.
            config = prepared.config_for(self.mcp_id, config)
        client, remote_tools = await self._connect(config)
        local_names = _local_names(remote_tools)
        contributions = tuple(
            self._tool_contribution(client, remote, local_name)
            for remote, local_name in zip(remote_tools, local_names, strict=True)
        )
        self._client = client
        self._tool_views = tuple(
            {
                "id": f"{self.mcp_id}/{local_name}",
                "name": remote.name,
                "display_name": remote.display_name,
                **_annotation_metadata(remote.annotations),
            }
            for remote, local_name in zip(remote_tools, local_names, strict=True)
        )
        self._status = "healthy"
        assert context.scope is not None
        for contribution in contributions:
            context.scope.register_tool(contribution)
        context.scope.register_mcp_runtime(self._mcp_runtime_contribution())
        return None

    async def _connect(
        self, config: MCPServerConfig
    ) -> tuple[MCPClient, tuple[MCPRemoteTool, ...]]:
        """Connect to the server, retrying a transport that never opened.

        A refused connection or a stream reset by a proxy is a property of the
        path, not of the declaration: ``mcp.nws`` failed to start for a whole
        generation on a board where the very next attempt succeeded.  Only
        transport failures are retried, and only with a fresh client, so a
        server that answers with an error still fails on the first attempt.
        """

        for attempt in range(1, _CONNECT_ATTEMPTS + 1):
            client = self._client_factory(config)
            try:
                await client.start()
                remote_tools = await client.list_tools()
            except BaseException as exc:
                await self._close_after_failure(client)
                if attempt >= _CONNECT_ATTEMPTS or not is_transient_transport_error(exc):
                    raise
                delay = _CONNECT_BACKOFF_SECONDS[
                    min(attempt, len(_CONNECT_BACKOFF_SECONDS)) - 1
                ]
                logger.warning(
                    "MCP server %s did not connect (%s); retrying in %.1fs "
                    "(attempt %d/%d)",
                    self.mcp_id,
                    type(exc).__name__,
                    delay,
                    attempt + 1,
                    _CONNECT_ATTEMPTS,
                )
                await asyncio.sleep(delay)
            else:
                return client, remote_tools
        raise AssertionError("MCP connect retry loop must return or raise")

    @staticmethod
    async def _close_after_failure(client: MCPClient) -> None:
        """Tear down a half-open client without hiding the real failure."""

        try:
            await client.close()
        except BaseException:
            logger.debug("MCP client cleanup after a failed start", exc_info=True)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self._status = "stopped"
        if self._client is not None:
            await self._client.close()

    async def health(self) -> PluginHealth:
        prepared = self._preparation_health()
        if prepared is not None:
            return prepared
        if self._client is None or self._status == "stopped":
            return PluginHealth.unhealthy("MCP server is not connected")
        try:
            await self._client.list_tools()
        except Exception as exc:
            self._status = "unhealthy"
            return PluginHealth.unhealthy(
                f"MCP server health check failed ({type(exc).__name__})"
            )
        self._status = "healthy"
        return PluginHealth.healthy()

    def _preparation_health(self) -> PluginHealth | None:
        """What to report while this server is still being prepared.

        ``None`` means preparation has nothing to say: it is not owned here,
        the server is connected, or the Plugin was stopped.  Reporting
        ``preparing`` instead of ``unhealthy`` matters: the host stops an
        unhealthy Plugin, which would cancel the very work in flight.
        """

        if (
            self._preparation is None
            or self._client is not None
            or self._status == "stopped"
        ):
            return None
        state = self._preparation.state(self.mcp_id)
        if state is None:
            return None
        if state.status == MCP_PREPARATION_FAILED:
            self._status = MCP_PREPARATION_FAILED
            return PluginHealth.unhealthy(
                state.detail or "MCP server could not be started"
            )
        self._status = MCP_PREPARING
        # The phase is what makes a multi-minute preparation readable: an npm
        # install and a cold handshake are different waits, and "preparing"
        # alone never told the operator which one they were in.
        message = state.detail or (
            "MCP server dependencies are being installed"
            if state.phase == MCP_PHASE_INSTALLING
            else "MCP server is starting in the background"
        )
        return PluginHealth(status=MCP_PREPARING, message=message)

    async def reconfigure(self, _config: Mapping[str, object]) -> None:
        raise ValueError("MCP directory Plugins are configured through mcp.json")

    def _tool_contribution(
        self,
        client: MCPClient,
        remote: MCPRemoteTool,
        local_name: str,
    ) -> ToolContribution:
        source = str(self._source)
        qualified_id = f"{self.mcp_id}/{local_name}"
        remote_name = remote.name

        async def handler(context, arguments):
            if self._authorize is not None:
                selected = self._authorize(context.agent_id, self.mcp_id)
                if inspect.isawaitable(selected):
                    selected = await selected
                if not selected:
                    raise PermissionError(
                        f"Agent {context.agent_id} has not selected MCP server "
                        f"{self.mcp_id}"
                    )
            progress = context.progress
            try:
                result = await client.call_tool(
                    remote_name,
                    dict(arguments),
                    progress_callback=(
                        None
                        if progress is None
                        else _progress_forwarder(progress, qualified_id)
                    ),
                    meta={HOST_CONTEXT_KEY: _host_context(context)},
                )
            except Exception as exc:
                self._status = "degraded"
                raise ConnectionError(
                    f"MCP plugin {self.manifest.id} tool {remote_name!r} failed "
                    f"({type(exc).__name__})"
                ) from exc
            return ToolResult(
                content=result.content,
                metadata={
                    **dict(result.metadata),
                    "mcp_error": result.is_error,
                    "remote_name": remote_name,
                    "mcp_tool": qualified_id,
                    "run_id": context.run_id,
                    "session_id": context.session_id,
                    "agent_id": context.agent_id,
                },
            )

        return ToolContribution(
            plugin_id=self.manifest.id,
            spec=ToolSpec(
                name=local_name,
                description=remote.description,
                input_schema=remote.input_schema,
                metadata={
                    "qualified_id": qualified_id,
                    "display_name": remote.display_name,
                    "remote_name": remote_name,
                    "origin": "mcp",
                    **_annotation_metadata(remote.annotations),
                    "plugin_id": self.manifest.id,
                    "mcp_id": self.mcp_id,
                    "runtime_id": self.runtime_id,
                    "owner_plugin_id": self.owner_plugin_id,
                    "source_kind": self.source_kind,
                    "source_path": source,
                    "manifest_path": source,
                    "version": self.manifest.version,
                },
            ),
            handler=handler,
        )

    def _mcp_runtime_contribution(self) -> MCPRuntimeContribution:
        assert self._config is not None
        assert self._public_config is not None
        public_config = self._public_config
        command = (
            shlex.join((public_config.command, *public_config.args))
            if public_config.transport == "stdio"
            else public_config.url
        )
        return MCPRuntimeContribution(
            plugin_id=self.manifest.id,
            mcp_id=self.mcp_id,
            runtime_id=self.runtime_id,
            display_name=self._config.display_name or self.mcp_id,
            transport=self._config.transport,
            command=command,
            owner_plugin_id=self.owner_plugin_id,
            source_kind=self.source_kind,
            source_path=str(self._source),
            tools=self._tool_views,
        )


def _local_names(tools: tuple[MCPRemoteTool, ...]) -> tuple[str, ...]:
    remote_names = [tool.name for tool in tools]
    if len(remote_names) != len(set(remote_names)):
        raise ValueError("MCP server returned duplicate tool names")
    used: set[str] = set()
    names = []
    bases = []
    for remote_name in remote_names:
        base = _UNSAFE_NAME.sub("_", remote_name.lower()).strip("_") or "mcp_tool"
        if base[0].isdigit():
            base = f"mcp_{base}"
        bases.append(base)
    collisions = {base for base in bases if bases.count(base) > 1}
    for remote_name, base in zip(remote_names, bases, strict=True):
        selected = base
        if base in collisions:
            suffix = hashlib.sha256(remote_name.encode("utf-8")).hexdigest()[:8]
            selected = f"{base}_{suffix}"
        if selected in used:
            raise ValueError("MCP tool names collide after normalization")
        used.add(selected)
        names.append(selected)
    return tuple(names)


__all__ = ["MCPDirectoryPlugin"]
