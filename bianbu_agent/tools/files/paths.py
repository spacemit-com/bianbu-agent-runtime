from __future__ import annotations

from pathlib import Path


def resolve_path(workspace: Path, raw_path: object) -> Path:
    """Resolve a Tool path, preferring the workspace for relative input.

    The workspace is a default anchor, not a fence: a relative path resolves
    inside it, while an absolute path is used exactly as the caller wrote it
    (after ``~`` expansion and symlink resolution).
    """

    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("path must be a non-empty string")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(workspace).expanduser() / candidate
    return candidate.resolve()


def display_path(workspace: Path, path: Path) -> str:
    """Return a workspace-relative path when possible, else the absolute path."""

    try:
        return str(path.relative_to(Path(workspace).expanduser().resolve()))
    except ValueError:
        return str(path)


__all__ = ["display_path", "resolve_path"]
