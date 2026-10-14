"""Match an Agent's MCP selectors onto the servers this device installed."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from bianbu_agent.mcp.managed import managed_mcp_id


def resolve_mcp_selectors(
    selectors: Sequence[str],
    installed: Iterable[str],
) -> tuple[str, ...]:
    """Return the installed MCP IDs an Agent's selectors name.

    APM installs an MCP under the name its registry shows (``nws``) while this
    Runtime names the package ``mcp.nws``, and an expert author may declare
    either form.  An exact installed ID wins; otherwise the ``mcp.``-prefixed
    name is tried.  A selector this device does not have is kept as written so
    validation and the catalog can still report it missing.

    Only names are rewritten: ``*`` stays ``*`` and keeps meaning "every server
    this device has", so an Agent that declares a wildcard is never turned into
    a snapshot of today's installs.
    """

    known = {str(name) for name in installed}
    if tuple(selectors) == ("*",):
        return ("*",)
    resolved: list[str] = []
    for selector in selectors:
        candidates = (selector,) if "." in selector else (selector, managed_mcp_id(selector))
        match = next((name for name in candidates if name in known), selector)
        if match not in resolved:
            resolved.append(match)
    return tuple(resolved)


def selects_mcp(selectors: Sequence[str], mcp_id: str) -> bool:
    """Whether an Agent's MCP selectors pick this installed server.

    The counterpart to ``resolve_mcp_selectors`` for call sites that already
    hold one server ID: a wildcard takes everything, and a declaration written
    with the APM name (``nws``) still matches the installed ``mcp.nws``.
    """

    if tuple(selectors) == ("*",):
        return True
    return mcp_id in resolve_mcp_selectors(selectors, (mcp_id,))


__all__ = ["resolve_mcp_selectors", "selects_mcp"]
