"""Prove an installed MCP server can start before anyone depends on it.

A declaration that never connects is indistinguishable from a working one until
the first Run needs its tools, and by then the failure surfaces as a missing
tool rather than as the startup error that caused it.  This probe starts the
server once, lists its tools, and reports why it failed when it cannot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from bianbu_agent.mcp.config import MCPServerConfig, load_mcp_config


#: A transport that never opened is worth one more attempt: a refused
#: connection or a stream reset by a proxy is a property of the path, not of
#: the declaration.  ``mcp.nws`` failed to start for a whole generation on a
#: board where the very next attempt succeeded.
TRANSIENT_TRANSPORT_NAMES = frozenset({
    "BrokenResourceError",
    "ConnectError",
    "ConnectTimeout",
    "ClosedResourceError",
    "ConnectionResetError",
    "ConnectionError",
    "EndOfStream",
    "IncompleteRead",
    "NetworkError",
    "PoolTimeout",
    "ProtocolError",
    "ProxyError",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "TimeoutError",
    "TransportError",
    "WriteError",
})

#: Delays between those attempts.  The last one is reused for later attempts.
TRANSIENT_RETRY_BACKOFF_SECONDS = (0.5, 2.0)

#: The server answered an MCP handshake and listed its tools.
MCP_READY = "healthy"
#: The server was started and could not be reached.
MCP_UNHEALTHY = "unhealthy"
#: The caller asked for an install without a startup check.
MCP_UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class MCPProbeResult:
    status: str
    tools: tuple[str, ...]
    detail: str

    @property
    def ready(self) -> bool:
        return self.status == MCP_READY

    @property
    def verified(self) -> bool:
        return self.status != MCP_UNVERIFIED


async def probe_mcp_server(
    *,
    workspace: str | Path,
    root: str | Path | None = None,
    config: MCPServerConfig | None = None,
    attempts: int = 1,
) -> MCPProbeResult:
    """Start one MCP server, list its tools, and always shut it down again.

    ``attempts`` retries a transport that never opened; a server that answers
    with an error is never retried, because that answer is the result.
    """

    from bianbu_agent.mcp.client import MCPClient

    if (root is None) == (config is None):
        raise ValueError("probe_mcp_server needs exactly one of root or config")
    resolved = config
    if resolved is None:
        try:
            resolved = load_mcp_config(
                Path(root) / "mcp.json",  # type: ignore[arg-type]
                workspace=workspace,
            )
        except (OSError, ValueError) as exc:
            return MCPProbeResult(MCP_UNHEALTHY, (), failure_detail(exc))
    assert resolved is not None
    for attempt in range(1, max(1, int(attempts)) + 1):
        client = MCPClient(resolved)
        try:
            await client.start()
            tools = await client.list_tools()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - the probe reports any startup failure
            if attempt < attempts and is_transient_transport_error(exc):
                await asyncio.sleep(
                    TRANSIENT_RETRY_BACKOFF_SECONDS[
                        min(attempt, len(TRANSIENT_RETRY_BACKOFF_SECONDS)) - 1
                    ]
                )
                continue
            return MCPProbeResult(
                MCP_UNHEALTHY,
                (),
                failure_detail(exc, startup_timeout=resolved.startup_timeout_seconds),
            )
        finally:
            try:
                await client.close()
            except BaseException:  # noqa: BLE001 - a failed close must not mask the probe
                pass
        return MCPProbeResult(MCP_READY, tuple(tool.name for tool in tools), "")
    raise AssertionError("MCP probe retry loop must return or raise")


def is_transient_transport_error(error: BaseException) -> bool:
    """Whether a failed connection is worth one more attempt.

    The transport stack wraps its own failures: httpx raises ``ConnectError``,
    anyio raises ``BrokenResourceError``, and asyncio's TaskGroup reports them
    inside an ``ExceptionGroup``.  Match by name so the check keeps working
    across those layers without importing them at module scope, and never match
    cancellation: a cancelled Run must not be retried.
    """

    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return False
    if type(error).__name__ in TRANSIENT_TRANSPORT_NAMES:
        return True
    nested = getattr(error, "exceptions", ())
    if isinstance(nested, (tuple, list)):
        return any(
            is_transient_transport_error(item)
            for item in nested
            if isinstance(item, BaseException)
        )
    return False


def failure_detail(error: BaseException, *, startup_timeout: float | None = None) -> str:
    """One line an operator can act on, unwrapping the transport's grouping."""

    leaf = _leaf_error(error)
    message = str(leaf).strip()
    if isinstance(leaf, TimeoutError):
        budget = f" within {startup_timeout:.0f}s" if startup_timeout is not None else ""
        return f"MCP server did not answer the MCP handshake{budget}"
    if not message:
        return f"{type(leaf).__name__} while starting the MCP server"
    return f"{type(leaf).__name__}: {message}"


def _leaf_error(error: BaseException) -> BaseException:
    """The most actionable exception inside a transport-level ExceptionGroup.

    A task group reports every failure it saw, and the stderr pump's own I/O
    failure can be listed before the spawn failure that caused it.  An
    ``OSError`` (missing command, refused connection, timeout) is the cause
    worth printing; otherwise the first leaf is still the best answer.
    """

    leaves = _leaves(error)
    for leaf in leaves:
        if isinstance(leaf, OSError):
            return leaf
    return leaves[0]


def _leaves(error: BaseException) -> list[BaseException]:
    """Every non-group exception in ``error``, outermost first."""

    leaves: list[BaseException] = []
    queue: list[BaseException] = [error]
    while queue:
        current = queue.pop(0)
        if current is not error and current in leaves:
            continue
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, (tuple, list)):
            queue.extend(
                item for item in nested if isinstance(item, BaseException)
            )
            continue
        if current not in leaves:
            leaves.append(current)
    return leaves


__all__ = [
    "MCP_READY",
    "MCP_UNHEALTHY",
    "MCP_UNVERIFIED",
    "MCPProbeResult",
    "TRANSIENT_RETRY_BACKOFF_SECONDS",
    "TRANSIENT_TRANSPORT_NAMES",
    "failure_detail",
    "is_transient_transport_error",
    "probe_mcp_server",
]
