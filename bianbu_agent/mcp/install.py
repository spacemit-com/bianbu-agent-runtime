"""Prepare a managed MCP declaration before anything on disk changes.

The order matters: an npm-backed declaration is vendored, then the server is
started once to prove it answers, and only then is the declaration installed.
A server that cannot start therefore never becomes an installed MCP that the
Runtime has to explain away later.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from pathlib import Path

from bianbu_agent.home import AgentHome
from bianbu_agent.mcp.managed import (
    ManagedMCPDefinition,
    load_managed_mcp_config,
)
from bianbu_agent.mcp.readiness import (
    MCPProbeResult,
    MCP_UNVERIFIED,
    probe_mcp_server,
)
from bianbu_agent.mcp.vendor import (
    remove_vendored_server,
    vendor_npm_server,
    vendored_npm_server,
)


@dataclass(frozen=True, slots=True)
class PreparedManagedMCP:
    definition: ManagedMCPDefinition
    vendor_root: Path | None
    probe: MCPProbeResult


async def prepare_managed_mcp(
    definition: ManagedMCPDefinition,
    home: AgentHome,
    *,
    verify: bool = True,
) -> PreparedManagedMCP:
    """Vendor what the declaration needs, then prove it can start."""

    pinned, vendor_root = pin_npm_dependency(definition, home)
    if not verify:
        return PreparedManagedMCP(
            pinned,
            vendor_root,
            MCPProbeResult(MCP_UNVERIFIED, (), ""),
        )
    config = load_managed_mcp_config(pinned, workspace=home.workspace_dir)
    probe = await probe_mcp_server(workspace=home.workspace_dir, config=config)
    return PreparedManagedMCP(pinned, vendor_root, probe)


def pin_npm_dependency(
    definition: ManagedMCPDefinition,
    home: AgentHome,
    *,
    reuse: bool = True,
) -> tuple[ManagedMCPDefinition, Path | None]:
    """Replace an ``npx`` declaration with a local installation of its package.

    Returns the declaration to install and the directory holding the vendored
    tree, or the declaration unchanged when there is nothing to vendor.  An
    installation that is already complete is reused unless ``reuse`` is off,
    so preparing the same declaration twice does not redo the npm work.
    """

    if definition.transport != "stdio" or not definition.npm_package:
        return definition, None
    target = home.mcp_runtime_dir / definition.plugin_id
    vendored = (
        vendored_npm_server(definition.npm_package, target) if reuse else None
    )
    if vendored is None:
        vendored = vendor_npm_server(definition.npm_package, target)
    return _pinned_definition(definition, vendored), target


def vendored_pinned_definition(
    definition: ManagedMCPDefinition,
    home: AgentHome,
) -> tuple[ManagedMCPDefinition, Path] | None:
    """The declaration to run when its package is already installed.

    The same rewrite as :func:`pin_npm_dependency`, without ever invoking npm:
    callers that must not block use this to tell "ready to launch locally" from
    "still needs an installation".
    """

    if definition.transport != "stdio" or not definition.npm_package:
        return None
    target = home.mcp_runtime_dir / definition.plugin_id
    vendored = vendored_npm_server(definition.npm_package, target)
    if vendored is None:
        return None
    return _pinned_definition(definition, vendored), target


def _pinned_definition(
    definition: ManagedMCPDefinition,
    vendored: VendoredNpmServer,
) -> ManagedMCPDefinition:
    """Rewrite ``npx <package> <args...>`` into a local launch of that package.

    ``npx`` forwards everything after the package to the server, so the
    arguments survive the rewrite and only the launcher changes.
    """

    command = [
        str(vendored.interpreter),
        str(vendored.script),
        *definition.npm_arguments,
    ]
    return replace(
        definition,
        executable=command[0],
        args=tuple(command[1:]),
        command_line=shlex.join(command),
    )


def discard_prepared_managed_mcp(prepared: PreparedManagedMCP) -> None:
    """Drop anything a failed preparation left behind."""

    if prepared.vendor_root is not None:
        remove_vendored_server(prepared.vendor_root)


__all__ = [
    "PreparedManagedMCP",
    "discard_prepared_managed_mcp",
    "pin_npm_dependency",
    "prepare_managed_mcp",
    "vendored_pinned_definition",
]
