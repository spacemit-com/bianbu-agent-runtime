"""Unregister an Agent from securityd before its local declaration is removed.

Deleting an expert must not leave a stale identity in the Security console: the
Runtime and securityd own two registries, and the local one is only allowed to
disappear once securityd confirmed the removal.  The HTTP control plane keeps
its runtime-aware gate; the CLI and development tools use this module so every
deletion entry point follows the same rule.

The module deliberately stays importable without an execution client.  A CLI
``agent remove`` on a build host must not load the Worker graph or probe D-Bus;
it only needs to discover whether this process is running on a security
boundary, and that is decided from environment variables and installed client
paths before the heavier security modules are imported.
"""

from __future__ import annotations

import os
from pathlib import Path

_CLIENT_MODULES = ("bianbu_security_client.py",)
_CLIENT_DIRECTORIES = (
    "/usr/lib/bianbu-agent-securityd/client",
    "/usr/lib/bianbu-securityd/client",
)


class AgentSecurityUnregisterError(RuntimeError):
    """securityd did not confirm removal of the Agent identity."""


def _production_mode() -> bool:
    return (
        str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).strip().lower()
        == "production"
    )


def _client_directory() -> Path | None:
    configured = os.environ.get("BIANBU_SECURITY_CLIENT_DIR", "").strip()
    if configured:
        return Path(configured)
    home = os.environ.get("BIANBU_SECURITYD_HOME", "").strip()
    if home:
        return Path(home) / "client"
    return None


def _security_client_installed() -> bool:
    """Whether an installed security client can be used by this process."""

    directory = _client_directory()
    candidates = [directory] if directory is not None else []
    candidates.extend(Path(path) for path in _CLIENT_DIRECTORIES)
    return any(
        (candidate / module).is_file()
        for candidate in candidates
        for module in _CLIENT_MODULES
    )


def security_deletion_enabled() -> bool:
    """Whether this process must unregister an Agent before deleting it.

    A device install always carries a security client and either runs the
    packaged service (``BIANBU_RUNTIME_MODE=production``) or the development
    launcher, which exports ``BIANBU_SECURITY=1``.  A plain source checkout on
    a build host has no security boundary, so deletion stays local.
    """

    if _production_mode():
        return True
    if os.environ.get("BIANBU_SECURITY") == "1":
        return True
    return _security_client_installed()


async def unregister_agent_before_delete(
    agent_id: str, *, plugins: object = None
) -> bool:
    """Remove one Agent identity from securityd.

    Returns ``True`` when securityd confirmed the call.  Raises when a security
    boundary is configured and the client cannot reach it, so the caller never
    deletes the local declaration while the Security console keeps the Agent.

    ``plugins`` is the deleted Agent's Plugin selection.  An Agent that did not
    select ``bianbu.security`` never acquired a securityd identity, so deleting
    it must not contact securityd even on a device where the client is
    installed.  ``None`` preserves the legacy caller contract for callers that
    cannot resolve the declaration.
    """

    if plugins is not None:
        from bianbu_agent.security.enablement import security_plugin_selected

        if not security_plugin_selected(plugins):
            return False
    if not security_deletion_enabled():
        return False
    from bianbu_agent.security.agent_registration import SecurityAgentRegistration
    from bianbu_agent.security.security_runtime import SecurityRuntimeClient

    client = SecurityRuntimeClient(required=True)
    result = await SecurityAgentRegistration(client).unregister(agent_id)
    if result is None:
        raise AgentSecurityUnregisterError(
            f"securityd did not confirm unregistering Agent {agent_id}"
        )
    return True


__all__ = [
    "AgentSecurityUnregisterError",
    "security_deletion_enabled",
    "unregister_agent_before_delete",
]
