"""Which Agents opted into the optional ``bianbu.security`` Plugin.

Security is opt-in per Agent: an Agent that does not select the Plugin gets no
securityd registration, no DLP, no policy decision and no approval owned by
securityd.  The Plugin selection is the single switch; the Runtime must not
enable security merely because the plugin happens to be installed on disk.
"""
from __future__ import annotations

import os
from typing import Iterable

SECURITY_PLUGIN_ID = "bianbu.security"
SECURITY_AGENT_ENV = "BIANBU_SECURITY_AGENT_ENABLED"


def security_plugin_selected(plugins: object) -> bool:
    """Return whether a resolved or declared Plugin selection opts into security.

    ``materialize()`` resolves ``*`` and plugin-owned members before the run
    starts, but startup registration sees the stored declaration, so both the
    wildcard and the explicit id are accepted here.
    """

    if plugins is None:
        return False
    if isinstance(plugins, str):
        values: Iterable[object] = (plugins,)
    else:
        values = plugins  # type: ignore[assignment]
    for value in values:
        name = str(value).strip()
        if name in {"*", SECURITY_PLUGIN_ID}:
            return True
    return False


def security_agent_enabled(default: bool = True) -> bool:
    """Whether this runtime process serves a security-enabled Agent.

    The control plane sets ``BIANBU_SECURITY_AGENT_ENABLED=0`` for Agents that
    did not select the Plugin, so the Worker it spawns never reaches securityd.
    ``default`` stays ``True`` for embedders and existing tests; the product
    decides per Agent instead of relying on this default.
    """

    raw = os.environ.get(SECURITY_AGENT_ENV)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "off", "no"}


__all__ = [
    "SECURITY_AGENT_ENV",
    "SECURITY_PLUGIN_ID",
    "security_agent_enabled",
    "security_plugin_selected",
]
