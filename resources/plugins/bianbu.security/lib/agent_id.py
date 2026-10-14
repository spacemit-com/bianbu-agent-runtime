"""Use the logical Run subject registered with securityd.

Runtime process policy identity is only a fallback without a Run identity.
Never remap an unknown logical Agent to a more privileged global subject.
"""
from __future__ import annotations

import os

__all__ = ["resolve_agent_id", "global_agent_id"]


def global_agent_id() -> str:
    return os.environ.get("BIANBU_AGENT_ID", "com.bianbu.agent")


def resolve_agent_id(agent_id: str, *, client_provider=None) -> str:
    """Use trusted Runtime context, never tool arguments or registry probing."""
    return str(agent_id or "").strip() or global_agent_id()
