"""The AgentMemory identity the Runtime, the SDK and memoryd must agree on.

memoryd authorizes a call by comparing the caller's ``account_id`` and
``agent_name`` with the registration the Worker published for that process. The
registration is built from the Worker environment (``server/memory_registry``),
and the SDK falls back to the same environment when an MCP child is handed no
host context.

A made-up identity instead reads as an impersonation attempt and is refused with
``Unauthenticated``: on K3 (2026-09-22) the tool adapter filled the MCP host
context with the placeholders ``default`` / ``default-agent``, so every memory
write of that session was refused while the Hook path kept working — the Hooks
carry the Run's identity, the tools did not. Reading the environment in one
place keeps the registry entry and the call identity from drifting apart.
"""

from __future__ import annotations

import os

from bianbu_agent.plugins.contributions import DEFAULT_ACCOUNT_ID

# Owned by the WorkerSupervisor: it exports both to the Worker, which forwards
# them to command Hooks and to stdio MCP children.
ACCOUNT_ENV = "BIANBU_AGENT_MEMORY_ACCOUNT_ID"
AGENT_NAME_ENV = "BIANBU_AGENT_MEMORY_AGENT_NAME"

# memoryd's own registration default for the Agent name.
DEFAULT_AGENT_NAME = "default-agent"


def env_account_id(default: str = DEFAULT_ACCOUNT_ID) -> str:
    """Return the account this Worker is registered under."""

    return os.environ.get(ACCOUNT_ENV, "").strip() or default


def env_agent_name(default: str = DEFAULT_AGENT_NAME) -> str:
    """Return the Agent name this Worker is registered under."""

    return os.environ.get(AGENT_NAME_ENV, "").strip() or default
