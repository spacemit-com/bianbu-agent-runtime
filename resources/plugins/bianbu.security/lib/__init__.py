"""Shared security service clients used by the external Hook programs."""
from .agent_id import resolve_agent_id
from .bridge import HIGH_RISK_TOOLS, SecurityBridge, agent_security_mode_is_yolo, get_bridge
from .errors import SecurityDenied
from .mcp_transport import (
    clear_cache as clear_mcp_transport_cache,
    egress_info,
    is_remote_mcp,
    transport_of,
)
from .mapping import (
    UNMAPPED_CAPABILITY,
    resolve_capability,
    unmapped_decision,
    unmapped_policy,
)

__all__ = [
    "SecurityBridge",
    "get_bridge",
    "SecurityDenied",
    "resolve_capability",
    "resolve_agent_id",
    "HIGH_RISK_TOOLS",
    "agent_security_mode_is_yolo",
    # D-5 未映射工具处置策略
    "UNMAPPED_CAPABILITY",
    "unmapped_policy",
    "unmapped_decision",
    # D-9 远端 MCP 数据出境
    "egress_info",
    "is_remote_mcp",
    "transport_of",
    "clear_mcp_transport_cache",
]
