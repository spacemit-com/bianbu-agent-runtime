"""Command-oriented management for standalone MCP servers.

``add`` is the verb AgentHub uses to download an MCP server, and AgentHub
kills any Agent CLI call after 30 seconds.  Preparing an npm-backed server
needs far longer than that (the first ``bilibili-mcp-js`` install downloads
116MB), so ``add`` only registers the declaration and returns.  The Runtime
prepares and starts every declared server in the background on each Worker
start, which is also where a server that cannot start now shows up as
``detail`` instead of as a failed install.
"""

from __future__ import annotations

import argparse
import getpass
import shlex
import tempfile
from dataclasses import replace
from pathlib import Path

from bianbu_agent.cli.output import emit, plain
from bianbu_agent.home import AgentHome
from bianbu_agent.mcp import (
    MCPPackageManager,
    parse_managed_mcp,
    parse_remote_mcp,
    write_managed_mcp_package,
)
from bianbu_agent.mcp.credentials import write_secret
from bianbu_agent.mcp.managed import DEFAULT_STARTUP_TIMEOUT_SECONDS, ManagedMCPDefinition
from bianbu_agent.mcp.preparation import MCP_PREPARING
from bianbu_agent.mcp.vendor import remove_vendored_server


async def mcp_command(home: AgentHome, args: argparse.Namespace) -> int:
    manager = MCPPackageManager(home)
    if args.action == "list":
        value = manager.list()
    elif args.action == "add":
        value = await _add_server(home, manager, args)
    elif args.action == "get":
        value = manager.inspect(_public_mcp_id(args.name))
    elif args.action == "remove":
        mcp_id = _public_mcp_id(args.name)
        deleted = manager.uninstall(mcp_id)
        if deleted:
            # The declaration is gone, so its dependency tree has no reader
            # left; leaving it behind would keep hundreds of megabytes on a
            # board that is short on disk.
            remove_vendored_server(home.mcp_runtime_dir / mcp_id)
        value = {"deleted": deleted}
    elif args.action == "install":
        # The package directory names the MCP id, which is what APM staged.
        value = manager.install(args.source)
    elif args.action in {"login", "reauth"}:
        names = [args.name] if args.name and not args.all else [item.id for item in manager.list()]
        if not names:
            raise ValueError("an MCP name or --all is required")
        updated = []
        for name in names:
            short = name.removeprefix("mcp.")
            token = getpass.getpass(f"MCP token for {short}: ").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            if not token:
                raise ValueError("token must not be empty")
            variable = "MCP_" + "".join(c if c.isalnum() else "_" for c in short.upper()).strip("_") + "_API_KEY"
            write_secret(variable, token)
            updated.append(short)
        value = {"authorized": updated, "flow": args.flow}
    else:
        raise ValueError(f"unsupported mcp action: {args.action}")
    emit(value)
    return 0


async def _add_server(
    home: AgentHome,
    manager: MCPPackageManager,
    args: argparse.Namespace,
) -> dict[str, object]:
    definition = replace(
        _managed_definition(args),
        plugin_id=_public_mcp_id(args.name),
    )
    installed = manager.inspect(definition.plugin_id)
    if installed is not None and not args.upgrade:
        raise ValueError(f"MCP server already exists: {definition.plugin_id}")
    try:
        with tempfile.TemporaryDirectory(prefix="bianbu-managed-mcp-") as temporary:
            package = write_managed_mcp_package(
                definition,
                Path(temporary),
            )
            record = manager.install(package, upgrade=args.upgrade)
    except BaseException:
        raise
    return {
        **plain(record),
        # The declaration is installed; the server itself is started by the
        # Runtime, which reports what it found as MCP health.
        "status": MCP_PREPARING,
        "tools": [],
        "detail": "",
        "vendored": False,
        "startup_timeout_seconds": DEFAULT_STARTUP_TIMEOUT_SECONDS,
    }


def _managed_definition(args: argparse.Namespace) -> ManagedMCPDefinition:
    if args.url:
        if args.env:
            raise ValueError("--env can only be used with stdio")
        if args.args:
            raise ValueError("--args can only be used with stdio")
        definition = parse_remote_mcp(args.name, args.url, auth=args.auth)
        if args.auth in {"header", "oauth"}:
            token = getpass.getpass("MCP token: ").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            if not token:
                raise ValueError("token must not be empty")
            variable = "MCP_" + "".join(c if c.isalnum() else "_" for c in args.name.upper()).strip("_") + "_API_KEY"
            write_secret(variable, token)
        return definition
    command = [args.command, *args.args]
    if not args.command.strip():
        raise ValueError(
            "usage: bianbu-agent mcp add NAME --command COMMAND [--args ARG ...]"
        )
    return replace(
        parse_managed_mcp(args.name, shlex.join(command)),
        env=_parse_env(args.env),
    )


def _public_mcp_id(name: str) -> str:
    normalized = str(name).strip()
    if "." in normalized:
        return normalized.lower()
    return parse_managed_mcp(normalized, "true").plugin_id


def _parse_env(items: list[str]) -> dict[str, str]:
    values = {}
    for item in items:
        if "=" not in item:
            raise ValueError("--env values must use KEY=VALUE")
        key, value = item.split("=", 1)
        if not key or not key.replace("_", "a").isalnum() or not (key[0].isalpha() or key[0] == "_"):
            raise ValueError(f"invalid environment variable name: {key}")
        values[key] = value
    return values


__all__ = ["mcp_command"]
