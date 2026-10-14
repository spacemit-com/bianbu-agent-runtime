from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import re
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bianbu_agent.plugins import ToolCallContext


_CATALOG_PLUGIN_ID = "bianbu.catalog.filesystem"
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]*")
_CURRENT_TOOL_CALL: ContextVar[ToolCallContext | None] = ContextVar(
    "bianbu_agent_current_tool_call", default=None
)


def _tool_name(value: object) -> str:
    name = str(value).strip()
    if _TOOL_NAME.fullmatch(name) is None:
        raise ValueError("Tool name must match [a-z][a-z0-9_]*")
    return name


def _digest(value: object) -> str:
    digest = str(value).strip()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("source_digest must be a lowercase SHA-256 digest")
    return digest


@dataclass(frozen=True, slots=True)
class ToolSourceInspection:
    name: str
    version: str
    root: Path
    source_path: Path
    content_digest: str

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        source = Path(self.source_path).expanduser().resolve()
        if not source.is_relative_to(root):
            raise ValueError("Tool source path must be inside Tool root")
        object.__setattr__(self, "name", _tool_name(self.name))
        object.__setattr__(self, "version", str(self.version).strip())
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "source_path", source)
        object.__setattr__(self, "content_digest", _digest(self.content_digest))


@dataclass(frozen=True, slots=True)
class ToolLoadContext:
    workspace_root: Path
    services: Mapping[str, object] = field(default_factory=dict)
    config: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workspace_root",
            Path(self.workspace_root).expanduser().resolve(),
        )
        object.__setattr__(self, "services", MappingProxyType(dict(self.services)))
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))

    @property
    def workspace_path(self) -> Path:
        return self.workspace_root


@dataclass(frozen=True, slots=True)
class ToolPackageContext:
    name: str
    package_root: Path
    source_digest: str
    workspace_root: Path
    data_root: Path | None = None
    qualified_name: str = ""
    state_root: Path | None = None
    home_root: Path | None = None
    config: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))
        object.__setattr__(self, "name", _tool_name(self.name))
        object.__setattr__(self, "home_root", Path(self.home_root or self.workspace_root.parent).expanduser().resolve())
        qualified_name = str(self.qualified_name).strip() or self.name
        if qualified_name != self.name:
            namespace, separator, local_name = qualified_name.partition("/")
            if (
                separator != "/"
                or not namespace
                or "/" in local_name
                or local_name != self.name
            ):
                raise ValueError(
                    "qualified_name must be the Tool name or namespace/Tool name"
                )
        object.__setattr__(self, "qualified_name", qualified_name)
        object.__setattr__(
            self,
            "package_root",
            Path(self.package_root).expanduser().resolve(),
        )
        object.__setattr__(
            self,
            "workspace_root",
            Path(self.workspace_root).expanduser().resolve(),
        )
        object.__setattr__(self, "source_digest", _digest(self.source_digest))
        data_root = (
            self.package_root / "data" if self.data_root is None else self.data_root
        )
        object.__setattr__(
            self,
            "data_root",
            Path(data_root).expanduser().resolve(),
        )
        state_root = (
            self.workspace_root.parent / "runtime" / "tool-state"
            if self.state_root is None
            else self.state_root
        )
        object.__setattr__(
            self,
            "state_root",
            Path(state_root).expanduser().resolve(),
        )

    @contextmanager
    def bind_call(self, call: ToolCallContext) -> Iterator[None]:
        token = _CURRENT_TOOL_CALL.set(call)
        try:
            yield
        finally:
            _CURRENT_TOOL_CALL.reset(token)

    @property
    def call(self) -> ToolCallContext:
        call = _CURRENT_TOOL_CALL.get()
        if call is None:
            raise RuntimeError("Tool call metadata requested outside an active call")
        return call


__all__ = [
    "ToolLoadContext",
    "ToolPackageContext",
    "ToolSourceInspection",
]
