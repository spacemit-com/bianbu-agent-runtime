"""MCP sessions, with generation-owned persistent connections for Workers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED

from bianbu_agent.mcp.config import MCPServerConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MCPRemoteTool:
    name: str
    display_name: str
    description: str
    input_schema: Mapping[str, object]
    annotations: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
        object.__setattr__(self, "annotations", MappingProxyType(dict(self.annotations)))


@dataclass(frozen=True, slots=True)
class MCPCallResult:
    content: str
    is_error: bool = False
    structured_content: Mapping[str, object] | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        structured = self.structured_content
        if structured is not None:
            object.__setattr__(
                self,
                "structured_content",
                MappingProxyType(dict(structured)),
            )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


SessionFactory = Callable[[MCPServerConfig], Any]
# MCP progress 通知：(已完成, 总量|None, 文案|None) —— 与 mcp SDK 的
# ProgressFnT 对齐，total 允许缺省表示进度没有已知分母。
ProgressCallback = Callable[[float, float | None, str | None], Any]

# stdio MCP Server 是 Runtime 亲手启动的子进程，但它的生命周期不由会话对象掌握：
# 子进程被外部杀死（OOM、第三方崩溃、运维 kill）后流会关闭，持有会话的任务却收不到
# 任何通知，下一次调用只会拿到 ClosedResourceError。会话所有者必须自己发现并重建
# 连接，否则一个死掉的子进程会让这个 MCP Server 直到 Runtime 重启前都不可用。
# KEEPALIVE_SECONDS 是空闲时的探测间隔；重建用有上限的退避，避免长期不可用的 Server
# 变成忙等；只有活过一个完整稳定期的连接才把退避清零。
KEEPALIVE_SECONDS = 30.0
_STABLE_SESSION_SECONDS = 60.0
_RECONNECT_DELAYS = (0.5, 2.0, 5.0, 15.0, 30.0)

# 传输已经消失：SDK 用这些异常结束请求，或把在途请求标成 CONNECTION_CLOSED。
# 这类失败必须重建会话，但已经交给传输的请求不能重放。
_TRANSPORT_LOST_NAMES = frozenset({
    "BrokenPipeError",
    "BrokenResourceError",
    "ClosedResourceError",
    "ConnectionResetError",
    "EndOfStream",
})
# 请求还没交给传输就被拒绝：子进程不可能执行过它，重试不会让工具跑两遍。
_UNSENT_REQUEST_NAMES = frozenset({
    "BrokenPipeError",
    "BrokenResourceError",
    "ClosedResourceError",
    "ConnectionResetError",
})


def _consume_future_exception(future: asyncio.Future) -> None:
    """Keep a background readiness future's later retrieval from being a leak.

    ``start()`` owns the first connection and reads its failure itself; a loop
    restarted by a later call publishes its readiness to nobody, and an
    unretrieved exception would only show up as asyncio noise at collection.
    """

    if not future.cancelled():
        future.exception()


def _named_error(error: BaseException, names: frozenset[str]) -> bool:
    """按异常名判断失败类型，穿透 anyio/asyncio 的 ExceptionGroup 包装。

    传输层（httpx、anyio、asyncio）各自包装自己的失败，按名字匹配不依赖它们的
    导入，与 ``bianbu_agent.mcp.plugin`` 的连接重试保持同一策略。取消永远不算
    传输失败：被取消的 Run 不该重连。
    """

    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return False
    if type(error).__name__ in names:
        return True
    nested = getattr(error, "exceptions", ())
    if isinstance(nested, (tuple, list)):
        return any(
            _named_error(item, names)
            for item in nested
            if isinstance(item, BaseException)
        )
    return False


def _transport_lost(error: BaseException) -> bool:
    """失败是否说明当前会话的传输已经没了。"""

    if isinstance(error, McpError):
        return getattr(getattr(error, "error", None), "code", None) == CONNECTION_CLOSED
    return _named_error(error, _TRANSPORT_LOST_NAMES)


def _request_not_sent(error: BaseException) -> bool:
    """失败是否发生在请求交给传输之前（因此可以安全重试）。"""

    return _named_error(error, _UNSENT_REQUEST_NAMES)


class MCPClient:
    def __init__(
        self,
        config: MCPServerConfig,
        *,
        session_factory: SessionFactory | None = None,
        keepalive_seconds: float | None = KEEPALIVE_SECONDS,
    ) -> None:
        self.config = config
        self._session_factory = session_factory or _open_session
        self._keepalive_seconds = keepalive_seconds
        self._owner: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._session = None
        self._connection_error: BaseException | None = None
        self._closed = False
        self._changed: asyncio.Event = asyncio.Event()
        self._invalidate = asyncio.Event()

    def _arm_owner(self) -> None:
        self._invalidate.clear()
        self._ready = asyncio.get_running_loop().create_future()
        self._ready.add_done_callback(_consume_future_exception)
        self._owner = asyncio.create_task(self._own_session())

    async def start(self) -> None:
        if self._owner is None:
            self._arm_owner()
        try:
            # 启动预算和调用预算是两件事：一个 stdio server 可能要解析依赖、
            # 解包或起解释器，而一次 tools/call 只需要 30s。共用一个数字时，
            # 慢启动被误判成坏 server，这一代 generation 就永久少了这台工具。
            async with asyncio.timeout(self.config.startup_timeout_seconds):
                await asyncio.shield(self._ready)
        except BaseException:
            await self.close()
            raise
        if self._session is None:
            raise ConnectionError("MCP connection is closed") from self._connection_error

    def _notify_session_change(self) -> None:
        """Wake every caller waiting for the owned session to be (re)published."""

        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def _own_session(self) -> None:
        # MCP transports contain AnyIO cancel scopes. Their enter and exit must
        # run in this same task even when setup, calls and stop use other tasks.
        # The loop is what makes a killed MCP child recoverable: each iteration
        # owns one session for its whole lifetime.
        attempt = 0
        while not self._closed:
            started = time.monotonic()
            try:
                async with self._session_factory(self.config) as session:
                    await session.initialize()
                    self._session = session
                    self._connection_error = None
                    self._notify_session_change()
                    if not self._ready.done():
                        self._ready.set_result(None)
                    await self._supervise(session)
            except BaseException as exc:
                self._session = None
                self._notify_session_change()
                self._connection_error = exc
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if not self._ready.done():
                    # 第一次连接是 start() 的启动结果：报给调用方，不背着它重试。
                    if isinstance(exc, asyncio.CancelledError) or self._closed:
                        self._ready.cancel()
                    else:
                        self._ready.set_exception(exc)
                    return
                if self._closed or isinstance(exc, asyncio.CancelledError):
                    return
                logger.warning(
                    "MCP connection to %s ended (%s); reconnecting",
                    self.config.display_name or self.config.transport,
                    type(exc).__name__,
                )
            else:
                # _supervise returns only to replace the session or to stop.
                self._session = None
                self._notify_session_change()
            if self._closed:
                return
            if time.monotonic() - started >= _STABLE_SESSION_SECONDS:
                attempt = 0
            await asyncio.sleep(_RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)])
            attempt += 1

    async def _supervise(self, session: object) -> None:
        """Hold a published session until it dies or must be replaced.

        子进程被杀死时传输会静默失效，会话对象看起来仍然可用。周期性 ping 是客户端
        唯一能不等下一次工具调用就发现它的手段；JSON-RPC 错误说明传输还活着，只有
        探测本身失败才判定会话已死。
        """

        ping = getattr(session, "send_ping", None)
        while True:
            if self._keepalive_seconds is None:
                await self._invalidate.wait()
            else:
                try:
                    async with asyncio.timeout(self._keepalive_seconds):
                        await self._invalidate.wait()
                except TimeoutError:
                    pass
            if self._closed:
                return
            if self._invalidate.is_set():
                self._invalidate.clear()
                return
            if ping is None:
                continue
            try:
                async with asyncio.timeout(self.config.timeout_seconds):
                    await ping()
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except McpError as exc:
                # 服务端回答了 JSON-RPC 错误：传输活着，只是不接受 ping。
                logger.debug("MCP keepalive ping was rejected by the server: %s", exc)
            except Exception as exc:
                self._connection_error = exc
                logger.warning(
                    "MCP keepalive ping failed (%s); reconnecting",
                    type(exc).__name__,
                )
                return

    async def _owned_session(self):
        """Return the session held by the reconnect loop, re-arming it if it stopped.

        维护循环只会因为 close() 或被取消而结束（传输自己的 cancel scope 也会取消
        它）。被取消不是客户端的终态：下一次使用重新武装循环，否则一次取消就会让这个
        MCP Server 一直不可用到 Runtime 重启。
        """

        if self._owner is None:
            return None
        if self._owner.done() and not self._closed:
            logger.warning("MCP session owner had stopped; restarting it")
            self._arm_owner()
        session = self._session
        if session is not None:
            return session
        return await self._wait_for_session()

    async def _wait_for_session(self, *, exclude: object | None = None):
        """Wait for the reconnect loop to publish a session, within one request budget."""

        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                while True:
                    session = self._session
                    if session is not None and session is not exclude:
                        return session
                    if self._closed or (self._owner is not None and self._owner.done()):
                        return None
                    await self._changed.wait()
        except TimeoutError:
            return None

    async def _run(self, operation: Callable[[Any], Any], *, retry_not_sent: bool = False):
        """Run ``operation`` on a live session, replacing a transport that is gone.

        已交给传输的失败绝不重放：子进程可能已经执行过那个工具，重试会让副作用翻倍。
        只有从没交给传输的请求（写侧已经关闭）才重建会话后重跑一次。
        """

        session = await self._owned_session()
        if session is None:
            if self._owner is not None:
                raise ConnectionError("MCP connection is closed") from self._connection_error
            # One-off callers can use the client without retaining a connection.
            async with self._session_factory(self.config) as one_off:
                await one_off.initialize()
                return await operation(one_off)
        try:
            return await operation(session)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if self._owner is None or not _transport_lost(exc):
                raise
            # 让维护循环立刻放弃这个会话；下一次调用会拿到重建后的连接。
            self._invalidate.set()
            if not retry_not_sent or not _request_not_sent(exc):
                raise
            replacement = await self._wait_for_session(exclude=session)
            if replacement is None:
                raise
            return await operation(replacement)

    async def close(self) -> None:
        self._closed = True
        owner = self._owner
        if owner is not None:
            if not owner.done():
                owner.cancel()
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError:
                if not owner.cancelled():
                    raise
        self._notify_session_change()

    async def list_tools(self) -> tuple[MCPRemoteTool, ...]:
        async def fetch(session):
            result = await session.list_tools()
            remote_items = list(result.tools)
            cursor = getattr(result, "nextCursor", None)
            seen_cursors: set[str] = set()
            while cursor:
                if cursor in seen_cursors:
                    raise ValueError("MCP tool pagination repeated a cursor")
                seen_cursors.add(cursor)
                result = await session.list_tools(cursor=cursor)
                remote_items.extend(result.tools)
                cursor = getattr(result, "nextCursor", None)
            return remote_items

        async with asyncio.timeout(self.config.timeout_seconds):
            remote_items = await self._run(fetch, retry_not_sent=True)
        tools = []
        for item in remote_items:
            schema = getattr(item, "inputSchema", None)
            if schema is None:
                schema = getattr(item, "input_schema", None)
            if not isinstance(schema, Mapping) or schema.get("type") != "object":
                raise ValueError(f"MCP tool {item.name!r} must expose an object input schema")
            title = str(getattr(item, "title", None) or "").strip()
            remote_annotations = getattr(item, "annotations", None)
            annotations = _annotation_flags(remote_annotations)
            annotation_title = str(
                _annotation_value(remote_annotations, "title") or ""
            ).strip()
            tools.append(
                MCPRemoteTool(
                    name=str(item.name),
                    display_name=title or annotation_title or str(item.name),
                    description=str(getattr(item, "description", None) or ""),
                    input_schema=dict(schema),
                    annotations=annotations,
                )
            )
        return tuple(tools)

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        progress_callback: ProgressCallback | None = None,
        meta: Mapping[str, object] | None = None,
    ) -> MCPCallResult:
        async def invoke(session):
            return await session.call_tool(
                str(name),
                arguments=dict(arguments),
                progress_callback=progress_callback,
                meta=dict(meta) if meta else None,
            )

        async with asyncio.timeout(self.config.timeout_seconds):
            result = await self._run(invoke, retry_not_sent=True)
        return _convert_result(result)


_ANNOTATION_ALIASES: Mapping[str, tuple[str, ...]] = {
    "title": ("title",),
    "read_only": ("readOnlyHint", "read_only_hint"),
    "destructive": ("destructiveHint", "destructive_hint"),
    "idempotent": ("idempotentHint", "idempotent_hint"),
    "open_world": ("openWorldHint", "open_world_hint"),
}

# MCP 注解是服务端自述，只当作提示使用：只认已知字段与布尔值，其它一律丢弃，
# 避免把一个没见过的键透传成 Runtime 语义。
_ANNOTATION_FLAGS = ("read_only", "destructive", "idempotent", "open_world")


def _annotation_value(annotations: object, key: str) -> object | None:
    if annotations is None:
        return None
    for alias in _ANNOTATION_ALIASES.get(key, (key,)):
        value = (
            annotations.get(alias)
            if isinstance(annotations, Mapping)
            else getattr(annotations, alias, None)
        )
        if value is not None:
            return value
    return None


def _annotation_flags(annotations: object) -> dict[str, object]:
    """把 MCP ToolAnnotations 归一成 Runtime 认识的布尔字段子集。

    ``title`` 不在这里产出：它已经由 ``display_name`` 承载，放进注解字典只会
    让同一个值有两条来源。
    """

    if annotations is None:
        return {}
    flags: dict[str, object] = {}
    for name in _ANNOTATION_FLAGS:
        value = _annotation_value(annotations, name)
        if isinstance(value, bool):
            flags[name] = value
    return flags


def _apply_mcp_sandbox(config: MCPServerConfig) -> tuple[str, list[str]]:
    """按沙箱配置决定是否把 MCP 子进程包进 bwrap（INT-RT-16）。

    沙箱配置来自环境变量而非 AgentSpec —— MCP session 在此处打开时拿不到
    Agent 上下文（config 里只有 server 自身信息）。由 service_runtime 在
    加载 Agent 时把生效配置写入环境，这里读取。

    配了沙箱但 bwrap 不可用时**抛异常拒绝启动**，不静默裸跑 ——
    以为隔离生效却没生效，比明确失败危险得多。
    """
    import json
    import os

    raw = os.environ.get("BIANBU_MCP_SANDBOX")
    if not raw:
        return config.command, list(config.args)
    try:
        from bianbu_agent.security.security_sandbox import (
            SandboxConfig,
            wrap_mcp_command,
        )

        payload = json.loads(raw)
        mcp_id = str(payload.get("_mcp_id") or config.display_name or "")
        cfg = SandboxConfig(
            mode=str(payload.get("mode", "off")),
            net=bool(payload.get("net", True)),
            rw_dirs=tuple(payload.get("rw_dirs") or ()),
            mcp_servers=tuple(payload.get("mcp_servers") or ()),
        )
        return wrap_mcp_command(config.command, list(config.args), cfg, mcp_id)
    except ImportError:
        return config.command, list(config.args)


# Worker execution identity and AgentMemory addressing. Both are properties of
# the Worker that starts the MCP child, never of the manifest.
_RESERVED_ENV = (
    "BIANBU_RUNTIME_ROLE",
    "BIANBU_WORKER_EFFECTIVE_MODE",
    "BIANBU_SANDBOX_ACTIVE",
)
_MEMORY_ENV = (
    "BIANBU_AGENT_MEMORY_ENABLED",
    "BIANBU_AGENT_MEMORY_SOCKET",
    "BIANBU_AGENT_MEMORY_ACCOUNT_ID",
    "BIANBU_AGENT_MEMORY_AGENT_NAME",
)


def _stdio_environment(config: MCPServerConfig, *, sandboxed: bool) -> dict[str, str]:
    # The MCP SDK inherits only a minimal safe environment. Preserve the
    # supervisor's execution identity without forwarding unrelated secrets or
    # allowing a manifest to claim Host privileges. AgentMemory addressing
    # travels with it: the memory MCP server reaches the same daemon and
    # account as the command Hooks, which receive the Worker environment.
    reserved = _RESERVED_ENV + _MEMORY_ENV
    env = {key: value for key, value in config.env.items() if key not in reserved}
    env.update({key: os.environ[key] for key in reserved if key in os.environ})
    # stdio MCP servers are direct Worker children, so they reach memoryd
    # through the same Control-attested registration broker as command Hooks.
    # The path is Runtime-supplied and a manifest cannot declare it.
    from bianbu_agent.server.hook_memory_registration import (
        REGISTRATION_SOCKET_ENV,
        registration_environment,
    )
    env.pop(REGISTRATION_SOCKET_ENV, None)
    env.update(registration_environment(os.environ))
    if sandboxed:
        env.update(BIANBU_WORKER_EFFECTIVE_MODE="sandbox", BIANBU_SANDBOX_ACTIVE="1")
    return env


@asynccontextmanager
async def _open_session(config: MCPServerConfig) -> AsyncIterator[ClientSession]:
    if config.transport == "stdio":
        # bianbu-security（INT-RT-16）：mode=mcp 时把子进程包进 bwrap。
        # stdio MCP server 是第三方代码、以服务用户身份启动，是隔离收益最高的一环。
        command, args = _apply_mcp_sandbox(config)
        parameters = StdioServerParameters(
            command=command,
            args=args,
            env=_stdio_environment(config, sandboxed=(command, args) != (config.command, list(config.args))),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                yield session
        return
    async with httpx.AsyncClient(
        headers=dict(config.headers),
        timeout=httpx.Timeout(config.timeout_seconds),
        follow_redirects=True,
    ) as http_client:
        connector = sse_client if config.transport == "sse" else streamable_http_client
        async with connector(config.url, http_client=http_client) as streams:
            read, write = streams[:2]
            async with ClientSession(read, write) as session:
                yield session


def _convert_result(result: object) -> MCPCallResult:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None and not isinstance(structured, Mapping):
        raise ValueError("MCP structuredContent must be an object")
    safe_structured = (
        _sanitize_binary(dict(structured)) if structured is not None else None
    )
    blocks = tuple(getattr(result, "content", ()) or ())
    documents = tuple(_block_document(block) for block in blocks)
    if safe_structured is not None:
        content = _json(safe_structured)
    elif len(blocks) == 1 and documents[0].get("type") == "text":
        content = str(documents[0].get("text") or "")
    else:
        content = _json(documents)
    metadata = getattr(result, "meta", None)
    if metadata is None:
        metadata = getattr(result, "_meta", None)
    return MCPCallResult(
        content=content,
        is_error=bool(
            getattr(result, "isError", getattr(result, "is_error", False))
        ),
        structured_content=(
            dict(safe_structured) if isinstance(safe_structured, Mapping) else None
        ),
        metadata=(dict(metadata) if isinstance(metadata, Mapping) else {}),
    )


def _block_document(block: object) -> dict[str, object]:
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        value = dump(mode="json", by_alias=True, exclude_none=True)
    elif isinstance(block, Mapping):
        value = dict(block)
    else:
        raise ValueError("MCP content block cannot be serialized")
    if not isinstance(value, dict):
        raise ValueError("MCP content block must serialize to an object")
    sanitized = _sanitize_binary(value)
    if not isinstance(sanitized, dict):
        raise ValueError("MCP content block must remain an object")
    return sanitized


def _sanitize_binary(value: object) -> object:
    if isinstance(value, Mapping):
        return _without_binary_data(dict(value))
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_sanitize_binary(item) for item in value]
    return value


def _without_binary_data(document: dict[str, object]) -> dict[str, object]:
    block_type = str(document.get("type") or "").casefold()
    is_media_block = block_type in {
        "audio",
        "image",
        "input_audio",
        "input_image",
    }
    is_blob_resource = isinstance(document.get("blob"), str)
    if not is_media_block and not is_blob_resource:
        return {
            str(key): _sanitize_binary(value)
            for key, value in document.items()
        }
    data_key = next(
        (
            key
            for key in ("data", "base64", "blob")
            if isinstance(document.get(key), str)
        ),
        None,
    )
    if data_key is None:
        return {
            str(key): _sanitize_binary(value)
            for key, value in document.items()
        }
    data = str(document[data_key])
    sanitized = dict(document)
    sanitized.pop(data_key, None)
    sanitized["data_omitted"] = True
    sanitized["size_bytes"] = _base64_size(data)
    return sanitized


def _base64_size(value: str) -> int:
    normalized = "".join(value.split())
    padding = len(normalized) - len(normalized.rstrip("="))
    return max(0, (len(normalized) * 3) // 4 - padding)


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = ["MCPCallResult", "MCPClient", "MCPRemoteTool"]
