"""Managed stdio MCP packages created from console command input."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bianbu_agent.mcp.config import MCPServerConfig


_UNSAFE_SLUG = re.compile(r"[^a-z0-9_-]+")
_SHELL_PUNCTUATION = frozenset(";&|<>`")

# ``npx`` is the only launcher we can turn into a local installation.  Every
# other spelling (``npm exec``, ``npx -p``) either names a different package
# than the one it runs or resolves through the caller's npm state, so those
# declarations keep running through npm at start time.
_NPX_EXECUTABLES = frozenset({"npx", "npx.cmd"})
_NPX_PACKAGE_FLAGS = ("--package", "-p", "--call", "-c")
_REGISTRY_SPEC = re.compile(
    r"^@?[A-Za-z0-9][A-Za-z0-9._~-]*(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)?"
    r"(?:@[A-Za-z0-9][A-Za-z0-9._~+-]*)?$"
)

#: Managed stdio MCP servers get a longer startup budget than the 30s a tool
#: call may take: the first start of an npm-backed server used to download its
#: dependency tree, and even a vendored one pays process and interpreter setup.
DEFAULT_STARTUP_TIMEOUT_SECONDS = 120.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0


def managed_mcp_id(name: object) -> str:
    """The package ID this Runtime uses for an MCP whose APM name is ``name``.

    APM installs an MCP under the name the user sees in the registry (``nws``),
    while the Runtime directory and capability ID carry the ``mcp.`` prefix
    (``mcp.nws``).  Install and selection both go through this helper so the
    two forms can never drift apart.
    """

    display_name = _non_empty_text(name, "name")
    slug = _UNSAFE_SLUG.sub("-", display_name.lower()).strip("-_")
    if not slug:
        digest = hashlib.sha256(display_name.encode("utf-8")).hexdigest()[:8]
        slug = f"server-{digest}"
    return f"mcp.{slug}"


@dataclass(frozen=True, slots=True)
class ManagedMCPDefinition:
    plugin_id: str
    display_name: str
    executable: str = ""
    args: tuple[str, ...] = ()
    command_line: str = ""
    transport: str = "stdio"
    url: str = ""
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    #: The npm package this declaration runs, when it is one we can install.
    npm_package: str = ""
    #: Arguments ``npx`` would forward to that package's executable.
    npm_arguments: tuple[str, ...] = ()


def npx_package_invocation(
    executable: str,
    args: tuple[str, ...],
) -> tuple[str, tuple[str, ...]] | None:
    """Split ``npx [flags] <package> [args...]`` into package and arguments.

    ``None`` means the declaration cannot be turned into a local installation:
    either it is not ``npx``, or it names its package in a flag
    (``npx -p <package> <bin>``, ``npx --call``), where the positional argument
    is a different executable than the package being installed.  Those keep
    their ``npx`` spelling and rely on the startup budget instead.
    """

    if Path(executable).name.lower() not in _NPX_EXECUTABLES:
        return None
    if any(
        arg in _NPX_PACKAGE_FLAGS
        or arg.startswith(("--package=", "--call="))
        for arg in args
    ):
        return None
    for index, arg in enumerate(args):
        if arg.startswith("-"):
            continue
        spec = arg
        if not _REGISTRY_SPEC.match(spec):
            return None
        return spec, tuple(args[index + 1:])
    return None


def npm_package_name(spec: str) -> str:
    """The package directory name inside ``node_modules`` for one npm spec."""

    text = str(spec).strip()
    if not text:
        return ""
    if text.startswith("@"):
        scope, _, rest = text.partition("/")
        if not scope or not rest:
            return ""
        return f"{scope}/{rest.split('@', 1)[0]}"
    return text.split("@", 1)[0]


def parse_managed_mcp(name: object, command: object) -> ManagedMCPDefinition:
    display_name = _non_empty_text(name, "name")
    command_line = _non_empty_text(command, "command")
    if "\n" in command_line or "\r" in command_line:
        raise ValueError("shell syntax is not supported in MCP commands")
    if "$(" in command_line or "`" in command_line:
        raise ValueError("shell syntax is not supported in MCP commands")
    try:
        lexer = shlex.shlex(
            command_line,
            posix=True,
            punctuation_chars=";&|<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = tuple(lexer)
    except ValueError as exc:
        raise ValueError("command contains invalid quoting") from exc
    if any(token and set(token) <= _SHELL_PUNCTUATION for token in tokens):
        raise ValueError("shell syntax is not supported in MCP commands")
    if not tokens:
        raise ValueError("command must include an executable")
    normalized = shlex.join(tokens)
    invocation = npx_package_invocation(tokens[0], tokens[1:])
    return ManagedMCPDefinition(
        plugin_id=managed_mcp_id(display_name),
        display_name=display_name,
        executable=tokens[0],
        args=tokens[1:],
        command_line=normalized,
        npm_package="" if invocation is None else invocation[0],
        npm_arguments=() if invocation is None else invocation[1],
    )


def parse_remote_mcp(name: object, url: object, *, auth: str = "none") -> ManagedMCPDefinition:
    display_name = _non_empty_text(name, "name")
    endpoint = _non_empty_text(url, "url")
    if not endpoint.startswith(("http://", "https://")):
        raise ValueError("url must be an HTTP(S) URL")
    if auth not in {"none", "header", "oauth"}:
        raise ValueError("auth must be none, header, or oauth")
    variable = "MCP_" + (re.sub(r"[^A-Z0-9]+", "_", display_name.upper()).strip("_") or "SERVER") + "_API_KEY"
    headers = {}
    if auth in {"header", "oauth"}:
        headers["Authorization"] = f"Bearer ${{{variable}}}"
    return ManagedMCPDefinition(
        plugin_id=managed_mcp_id(display_name), display_name=display_name,
        transport="streamable-http", url=endpoint, headers=headers,
    )


def write_managed_mcp_package(
    definition: ManagedMCPDefinition,
    root: Path,
) -> Path:
    package = Path(root) / definition.plugin_id
    try:
        package.mkdir()
    except FileExistsError as exc:
        raise FileExistsError(
            f"MCP server {definition.plugin_id} already exists"
        ) from exc
    (package / "mcp.json").write_text(
        json.dumps(managed_mcp_document(definition), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return package


def managed_mcp_document(definition: ManagedMCPDefinition) -> dict[str, object]:
    """The exact mcp.json body for one managed declaration.

    Install and the pre-install probe both build their input here so the bytes
    that were proven to start are the bytes that get installed.
    """

    document: dict[str, object] = {
        "display_name": definition.display_name,
        "transport": definition.transport,
        "timeout_seconds": DEFAULT_TOOL_TIMEOUT_SECONDS,
        "startup_timeout_seconds": DEFAULT_STARTUP_TIMEOUT_SECONDS,
    }
    if definition.transport == "stdio":
        document.update(command=definition.executable, args=list(definition.args))
        if definition.env:
            document["env"] = dict(definition.env)
    else:
        document["url"] = definition.url
        if definition.headers:
            document["headers"] = dict(definition.headers)
    return document


def load_managed_mcp_config(
    definition: ManagedMCPDefinition,
    *,
    workspace: str | Path,
) -> MCPServerConfig:
    """Resolve one managed declaration without writing it to disk."""

    from bianbu_agent.mcp.config import _load_mcp_config_bytes

    payload = json.dumps(managed_mcp_document(definition)).encode("utf-8")
    return _load_mcp_config_bytes(
        payload,
        workspace=workspace,
        environ=None,
        resolve_environment=True,
    )


def _non_empty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


__all__ = [
    "ManagedMCPDefinition",
    "load_managed_mcp_config",
    "managed_mcp_document",
    "npm_package_name",
    "npx_package_invocation",
    "managed_mcp_id",
    "parse_managed_mcp",
    "parse_remote_mcp",
    "write_managed_mcp_package",
]
