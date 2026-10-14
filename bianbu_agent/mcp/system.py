"""Read-only MCP declarations provided by system packages."""
from pathlib import Path

SYSTEM_MCP_ROOT = Path("/usr/share/bianbu-agent-runtime/mcps")


def system_mcp_root() -> Path:
    from bianbu_agent.system_resources import system_resource_dir

    if SYSTEM_MCP_ROOT != Path("/usr/share/bianbu-agent-runtime/mcps"):
        return SYSTEM_MCP_ROOT
    return system_resource_dir("mcps")
