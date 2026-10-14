"""Process-wide serialization for a directory of Model YAML records."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_LOCK_FILE_NAME = ".model-records.lock"


class ModelRecordFileLock:
    """Advisory lock shared by every Store and Resolver for one Model root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def _lock_root(self) -> Path:
        override = os.environ.get("BIANBU_MODEL_LOCK_PATH", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        return self._root

    @contextmanager
    def shared(self) -> Iterator[None]:
        with self._acquire(fcntl.LOCK_SH):
            yield

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._acquire(fcntl.LOCK_EX):
            yield

    @contextmanager
    def _acquire(self, operation: int) -> Iterator[None]:
        lock_root = self._lock_root
        lock_root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise ValueError(f"unsafe Model record directory: {self._root}")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(lock_root / _LOCK_FILE_NAME, flags, 0o600)
        except OSError as exc:
            raise ValueError("unsafe Model record transaction lock") from exc
        try:
            entry = os.fstat(descriptor)
            if not stat.S_ISREG(entry.st_mode):
                raise ValueError("unsafe Model record transaction lock")
            os.fchmod(descriptor, 0o600)
            while True:
                try:
                    fcntl.flock(descriptor, operation)
                    break
                except InterruptedError:
                    continue
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


__all__ = ["ModelRecordFileLock"]
