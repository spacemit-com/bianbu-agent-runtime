"""Strict, secret-safe configuration for one directory-backed MCP server."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlparse
from bianbu_agent.mcp.credentials import load_environment


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*|workspace)\}")
_COMMON_KEYS = frozenset(
    {"display_name", "transport", "timeout_seconds", "startup_timeout_seconds"}
)
_STDIO_KEYS = _COMMON_KEYS | {"command", "args", "env"}
_HTTP_KEYS = _COMMON_KEYS | {"url", "headers"}


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    transport: str
    display_name: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    #: Budget for spawning and handshaking with the server.  A stdio server
    #: that resolves its own dependencies at start time needs far more room
    #: than one tool call, and sharing a single number made a slow first start
    #: look like a broken server.  Unset means "same as ``timeout_seconds``",
    #: which is what a declaration written before this field means.
    startup_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", tuple(self.args))
        if self.startup_timeout_seconds is None:
            object.__setattr__(
                self, "startup_timeout_seconds", self.timeout_seconds
            )
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


def load_mcp_config(
    path: str | Path,
    *,
    workspace: str | Path,
    environ: Mapping[str, str] | None = None,
) -> MCPServerConfig:
    return _load_mcp_config(
        path,
        workspace=workspace,
        environ=environ,
        resolve_environment=True,
    )


def validate_mcp_config(
    path: str | Path,
    *,
    workspace: str | Path,
) -> MCPServerConfig:
    """Validate an installable document without requiring runtime secrets."""

    return _load_mcp_config(
        path,
        workspace=workspace,
        environ={},
        resolve_environment=False,
    )


def validate_mcp_config_bytes(
    payload: bytes,
    *,
    workspace: str | Path,
) -> MCPServerConfig:
    """Validate the exact immutable bytes captured by a package snapshot."""

    return _load_mcp_config_bytes(
        payload,
        workspace=workspace,
        environ={},
        resolve_environment=False,
    )


def _load_mcp_config(
    path: str | Path,
    *,
    workspace: str | Path,
    environ: Mapping[str, str] | None,
    resolve_environment: bool,
) -> MCPServerConfig:
    source = Path(path).expanduser().resolve()
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"invalid mcp.json: {exc}") from exc
    return _load_mcp_config_bytes(
        payload,
        workspace=workspace,
        environ=environ,
        resolve_environment=resolve_environment,
    )


def _load_mcp_config_bytes(
    payload: bytes,
    *,
    workspace: str | Path,
    environ: Mapping[str, str] | None,
    resolve_environment: bool,
) -> MCPServerConfig:
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid mcp.json: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("mcp.json must contain an object")
    if not all(isinstance(key, str) for key in document):
        raise ValueError("mcp.json keys must be strings")

    transport = document.get("transport")
    if transport not in {"stdio", "streamable-http", "sse"}:
        raise ValueError("transport must be stdio, streamable-http, or sse")
    allowed = _STDIO_KEYS if transport == "stdio" else _HTTP_KEYS
    unknown = sorted(set(document) - allowed)
    if unknown:
        label = "key" if len(unknown) == 1 else "keys"
        raise ValueError(f"unknown mcp.json {label}: {', '.join(unknown)}")

    timeout = _positive_seconds(document.get("timeout_seconds", 30.0), "timeout_seconds")
    startup_timeout = _positive_seconds(
        document.get("startup_timeout_seconds", timeout),
        "startup_timeout_seconds",
    )
    values = load_environment() if environ is None else dict(environ)
    workspace_path = str(Path(workspace).expanduser().resolve())
    display_name = (
        _non_empty_text(document.get("display_name"), "display_name")
        if "display_name" in document
        else ""
    )

    if transport == "stdio":
        command = _non_empty_text(document.get("command"), "command")
        args = _string_array(document.get("args", []), "args")
        env = _string_mapping(document.get("env", {}), "env")
        return MCPServerConfig(
            transport=transport,
            display_name=display_name,
            command=_expand(command, workspace_path, values, resolve_environment),
            args=tuple(
                _expand(item, workspace_path, values, resolve_environment)
                for item in args
            ),
            env={
                key: _expand(value, workspace_path, values, resolve_environment)
                for key, value in env.items()
            },
            timeout_seconds=float(timeout),
            startup_timeout_seconds=startup_timeout,
        )

    url = _non_empty_text(document.get("url"), "url")
    url = _expand(url, workspace_path, values, resolve_environment)
    parsed = urlparse(url)
    deferred_url = (
        not resolve_environment
        and parsed.scheme == ""
        and _PLACEHOLDER.search(url) is not None
    )
    if not deferred_url and (
        parsed.scheme not in {"http", "https"} or not parsed.netloc
    ):
        raise ValueError("url must be an HTTP(S) URL")
    headers = _string_mapping(document.get("headers", {}), "headers")
    return MCPServerConfig(
        transport=transport,
        display_name=display_name,
        url=url,
        headers={
            key: _expand(value, workspace_path, values, resolve_environment)
            for key, value in headers.items()
        },
        timeout_seconds=float(timeout),
        startup_timeout_seconds=startup_timeout,
    )


def _positive_seconds(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{name} must be positive")
    return float(value)


def _non_empty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _string_array(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")
    return tuple(value)


def _string_mapping(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object of strings")
    if not all(isinstance(item, str) for item in value.values()):
        suffix = "values must be strings" if name == "env" else "must be an object of strings"
        raise ValueError(f"{name} {suffix}")
    return dict(value)


def _expand(
    value: str,
    workspace: str,
    environ: Mapping[str, str],
    resolve_environment: bool,
) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "workspace":
            return workspace
        if not resolve_environment:
            return match.group(0)
        if name not in environ:
            raise ValueError(f"environment variable is not set: {name}")
        return environ[name]

    return _PLACEHOLDER.sub(replace, value)


__all__ = [
    "MCPServerConfig",
    "load_mcp_config",
    "validate_mcp_config",
    "validate_mcp_config_bytes",
]
