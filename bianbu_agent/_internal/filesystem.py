from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile


class UnsafeFilesystemEntry(ValueError):
    """A filesystem entry a package reader must never follow or trust.

    Skipping such an entry would silently drop installed content that an
    attacker can redirect, so callers fail the whole read instead.
    """


def regular_files(root: Path) -> tuple[Path, ...]:
    """Return deterministic regular files and reject ambiguous filesystem entries.

    Walks with ``os.scandir`` instead of ``rglob`` + ``Path.resolve`` per entry:
    the walk is on every Run's critical path (capability verification), and on a
    K3 the old form spent most of its time in per-path ``lstat``/``resolve``
    syscalls (measured ~130ms for 249 files, twice per Run).  ``DirEntry``
    answers ``is_symlink``/``is_dir``/``is_file`` from the directory entry it
    already read, so the walk issues no per-entry syscalls.

    Symlinks are still rejected outright, which is also what makes escaping the
    root impossible: nothing outside ``root`` is ever followed.
    """

    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"capability root is not a directory: {root}")
    discovered: list[tuple[str, Path]] = []
    pending = [""]
    while pending:
        relative_dir = pending.pop()
        directory = root / relative_dir if relative_dir else root
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = (
                    f"{relative_dir}/{entry.name}" if relative_dir else entry.name
                )
                path = root / relative
                if entry.is_symlink():
                    raise UnsafeFilesystemEntry(
                        f"symbolic links are not allowed: {path}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    if entry.name != "__pycache__":
                        pending.append(relative)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise UnsafeFilesystemEntry(
                        f"unsupported capability filesystem entry: {path}"
                    )
                discovered.append((relative, path))
    discovered.sort(key=lambda item: item[0])
    files: list[Path] = []
    seen_names: set[str] = set()
    for relative, path in discovered:
        folded = relative.casefold()
        if folded in seen_names:
            raise ValueError(f"duplicate case-insensitive resource path: {relative}")
        seen_names.add(folded)
        files.append(path)
    return tuple(files)


def digest_directory(root: Path, files: tuple[Path, ...] | None = None) -> str:
    root = Path(root).expanduser().resolve()
    selected = files if files is not None else regular_files(root)
    digest = hashlib.sha256()
    for path in selected:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def create_file_if_absent(path: Path, payload: bytes) -> bool:
    """Atomically create *path* from durable bytes without replacing an existing file."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        _fsync_directory(target.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "UnsafeFilesystemEntry",
    "create_file_if_absent",
    "digest_directory",
    "regular_files",
]
