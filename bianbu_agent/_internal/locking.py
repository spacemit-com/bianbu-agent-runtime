from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path


class FilesystemLock:
    """Advisory cross-process lock for one resolved lock-file path."""

    def __init__(self, path: Path, *, exclusive: bool) -> None:
        self._path = Path(path).expanduser().resolve()
        self._exclusive = bool(exclusive)
        self._stream = None

    def __enter__(self):
        self._acquire()
        return self

    async def __aenter__(self):
        await asyncio.to_thread(self._acquire)
        return self

    def _acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self._path.open("a+b")
        operation = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
        fcntl.flock(self._stream.fileno(), operation)

    def __exit__(self, exc_type, exc, traceback) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await asyncio.to_thread(self.__exit__, exc_type, exc, traceback)


class CapabilityFilesystemLock(FilesystemLock):
    """Cross-process isolation for capability tree commits and readers."""

    def __init__(self, home: Path, *, exclusive: bool) -> None:
        override = os.environ.get("BIANBU_CAPABILITY_LOCK_PATH", "").strip()
        super().__init__(
            Path(override).expanduser().resolve()
            if override else Path(home).expanduser().resolve() / ".capability.lock",
            exclusive=exclusive,
        )


__all__ = ["CapabilityFilesystemLock", "FilesystemLock"]
