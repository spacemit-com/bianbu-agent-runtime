from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True, slots=True)
class AgentHome:
    """Stable filesystem layout owned by one Bianbu Agent runtime."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @property
    def inventory_path(self) -> Path:
        return self.root / "inventory.sqlite3"

    @property
    def plugins_dir(self) -> Path:
        return self.root / "plugins"

    @property
    def mcps_dir(self) -> Path:
        return self.root / "mcps"

    @property
    def mcp_runtime_dir(self) -> Path:
        """Vendored runtimes for MCP servers declared against a package manager.

        Kept outside ``mcps_dir`` because that tree holds immutable declaration
        bytes whose digest is verified on every read, while a vendored
        dependency tree is installed and replaced in place.
        """

        return self.root / "mcp-runtime"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def workspace_dir(self) -> Path:
        return self.root / "workspace"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def staging_dir(self) -> Path:
        return self.root / ".staging"

    @property
    def plugin_dirs(self) -> tuple[Path, ...]:
        return (self.plugins_dir,)

    @property
    def mcp_dirs(self) -> tuple[Path, ...]:
        return (self.mcps_dir,)

    @property
    def skill_dirs(self) -> tuple[Path, ...]:
        return (self.skills_dir,)

    def required_directories(self) -> tuple[Path, ...]:
        """Directories the Runtime owns and creates on every start.

        Extension roots (``tools``, ``skills``, ``plugins`` and ``mcps``)
        stay out of this list: the manager that writes them creates
        them on first use, so an install that never adds an extension keeps no
        empty directories behind.  ``cache_dir`` is only a documented location
        for host-owned caches: nothing in the Runtime writes it any more, so it
        is left to whoever needs it.
        """
        return (
            self.root,
            self.agents_dir,
            self.models_dir,
            self.sessions_dir,
            self.workspace_dir,
            self.logs_dir,
            self.staging_dir,
        )

    def ensure(self, directory: Path) -> Path:
        """Create one lazily owned Home directory on first use."""
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def prepare(self) -> AgentHome:
        for directory in self.required_directories():
            directory.mkdir(parents=True, exist_ok=True)
        return self


__all__ = ["AgentHome"]
