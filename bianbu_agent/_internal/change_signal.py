from __future__ import annotations

import asyncio


class AsyncRevisionSignal:
    """Lossless in-process wakeup for consumers that rescan durable state."""

    def __init__(self) -> None:
        self._revision = 0
        self._event = asyncio.Event()

    @property
    def revision(self) -> int:
        return self._revision

    def notify(self) -> None:
        event, self._event = self._event, asyncio.Event()
        self._revision += 1
        event.set()

    async def wait(self, after: int) -> int:
        while self._revision <= after:
            event = self._event
            if self._revision > after:
                break
            await event.wait()
        return self._revision


__all__ = ["AsyncRevisionSignal"]
