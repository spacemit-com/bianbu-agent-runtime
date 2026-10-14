"""Server-owned publication lifecycle for detached Session title Runs."""

from __future__ import annotations

import asyncio
import logging


logger = logging.getLogger(__name__)


class SessionTitleManager:
    """Publish current successful title work without blocking conversation Runs."""

    def __init__(self, runtime: object) -> None:
        self._runtime = runtime
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    def track(self, session_id: str, run_id: str, fallback: str) -> None:
        key = (str(session_id), str(run_id))
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._publish(key, fallback),
            name=f"session-title-{key[0]}-{key[1]}",
        )
        self._tasks[key] = task

    async def _publish(self, key: tuple[str, str], fallback: str) -> None:
        session_id, run_id = key
        try:
            handle = await self._runtime.runs.get(run_id)
            if handle is None:
                return
            result = await handle.wait()
            if str(result.status) != "completed":
                error = getattr(result, "error", None)
                logger.warning(
                    "Session title generation failed session=%s run=%s error=%s",
                    session_id,
                    run_id,
                    getattr(error, "message", None) or "unknown",
                )
                return
            current = await self._runtime.sessions.get(session_id)
            if current.title != fallback:
                return
            await self._runtime.sessions.rename(session_id, result.content)
        except asyncio.CancelledError:
            raise
        except (KeyError, ValueError):
            return
        except Exception:
            logger.exception(
                "Session title publication failed session=%s run=%s",
                session_id,
                run_id,
            )
        finally:
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)

    async def cancel_session(self, session_id: str) -> None:
        selected = str(session_id)
        tasks = [
            task for (owner, _), task in self._tasks.items()
            if owner == selected
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["SessionTitleManager"]
