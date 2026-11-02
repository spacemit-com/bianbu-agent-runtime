"""Read-only MCP declarations provided by system packages."""

from __future__ import annotations

from pathlib import Path

SYSTEM_MCP_ROOT = Path("/usr/share/bianbu-agent-runtime/mcps")


def system_mcp_root() -> Path:
    from bianbu_agent.system_resources import system_resource_dir

    if SYSTEM_MCP_ROOT != Path("/usr/share/bianbu-agent-runtime/mcps"):
        return SYSTEM_MCP_ROOT
    return system_resource_dir("mcps")


def system_mcp_roots() -> tuple[Path, ...]:
    """Return the system MCP root.

    Packages publish directly to ``mcps``; there is no drop-in root.  Keeping
    the tuple shape lets callers scan one contract while leaving room for a
    future root.
    """

    if SYSTEM_MCP_ROOT != Path("/usr/share/bianbu-agent-runtime/mcps"):
        return (SYSTEM_MCP_ROOT,)
    return (system_mcp_root(),)
