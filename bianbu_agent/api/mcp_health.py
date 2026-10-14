"""Application-owned, generation-bound MCP health snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from types import MappingProxyType
import time

from bianbu_agent.server.runtime import BianbuAgentRuntime


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MCPHealthSnapshot:
    generation_id: int
    statuses: Mapping[str, str]
    checked_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "statuses", MappingProxyType(dict(self.statuses)))


class MCPHealthCache:
    def __init__(
        self,
        runtime: BianbuAgentRuntime,
        *,
        ttl_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.runtime = runtime
        self.ttl_seconds = ttl_seconds
        self.monotonic = monotonic
        self.utcnow = utcnow
        self._lock = asyncio.Lock()
        self._snapshot: MCPHealthSnapshot | None = None
        self._checked_monotonic: float | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_generation_id: int | None = None
        self._retired_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def read(self) -> MCPHealthSnapshot:
        return await self._read_or_schedule(wait=False)

    async def refresh(self) -> MCPHealthSnapshot:
        return await self._read_or_schedule(wait=True)

    async def invalidate(self) -> None:
        await self._discard_snapshot_for_generation_change()

    async def close(self) -> None:
        await self._cancel_refresh_task()

    async def _read_or_schedule(self, *, wait: bool) -> MCPHealthSnapshot:
        while True:
            generation = self.runtime.plugin_runtime.current_generation
            async with self._lock:
                predecessors: tuple[asyncio.Task[None], ...] = ()
                if (
                    self._snapshot is None
                    or self._snapshot.generation_id != generation.id
                ):
                    self._replace_with_generation_snapshot(generation)
                    predecessors = self._retire_refresh_for_other_generation(
                        generation.id
                    )

                snapshot = self._snapshot
                assert snapshot is not None
                if self._closed or (not wait and self._is_fresh(snapshot)):
                    return snapshot

                task = self._refresh_task
                if task is not None and task.done():
                    self._refresh_task = None
                    self._refresh_generation_id = None
                    task = None
                if task is None:
                    predecessors = tuple(
                        set(predecessors) | self._retired_tasks
                    )
                    task = asyncio.create_task(
                        self._refresh_generation_after(
                            generation.id,
                            predecessors,
                        ),
                        name=f"mcp-health-generation-{generation.id}",
                    )
                    self._refresh_task = task
                    self._refresh_generation_id = generation.id
                    task.add_done_callback(self._refresh_finished)

            if not wait:
                # Give the scheduled coroutine a chance to enter health() without
                # ever waiting for the health operation itself.
                await asyncio.sleep(0)
                return snapshot

            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    continue
                raise

            async with self._lock:
                current_generation = self.runtime.plugin_runtime.current_generation
                if (
                    self._snapshot is not None
                    and self._snapshot.generation_id == current_generation.id
                    and self._snapshot.checked_at is not None
                ):
                    return self._snapshot

    def _replace_with_generation_snapshot(self, generation: object) -> None:
        generation_id = int(getattr(generation, "id"))
        mcps = getattr(generation, "mcps")
        self._snapshot = MCPHealthSnapshot(
            generation_id=generation_id,
            statuses={
                server.runtime_id: str(server.status)
                for server in mcps.values()
            },
            checked_at=None,
        )
        self._checked_monotonic = None

    def _is_fresh(self, snapshot: MCPHealthSnapshot) -> bool:
        return (
            snapshot.checked_at is not None
            and self._checked_monotonic is not None
            and self.monotonic() - self._checked_monotonic < self.ttl_seconds
        )

    async def _refresh_generation(self, generation_id: int) -> None:
        health = await self.runtime.plugin_runtime.health()
        statuses = {
            runtime_id: str(result.status)
            for runtime_id, result in health.items()
        }
        checked_at = self.utcnow()
        checked_monotonic = self.monotonic()
        async with self._lock:
            current_generation = self.runtime.plugin_runtime.current_generation
            if (
                not self._closed
                and current_generation.id == generation_id
                and self._snapshot is not None
                and self._snapshot.generation_id == generation_id
            ):
                self._snapshot = MCPHealthSnapshot(
                    generation_id=generation_id,
                    statuses=statuses,
                    checked_at=checked_at,
                )
                self._checked_monotonic = checked_monotonic

    async def _refresh_generation_after(
        self,
        generation_id: int,
        predecessors: tuple[asyncio.Task[None], ...],
    ) -> None:
        if predecessors:
            await asyncio.gather(*predecessors, return_exceptions=True)
        await self._refresh_generation(generation_id)

    def _retire_refresh_for_other_generation(
        self,
        generation_id: int,
    ) -> tuple[asyncio.Task[None], ...]:
        task = self._refresh_task
        if task is None or self._refresh_generation_id == generation_id:
            return ()
        self._refresh_task = None
        self._refresh_generation_id = None
        self._retired_tasks.add(task)
        task.cancel()
        return tuple(self._retired_tasks)

    def _refresh_finished(self, task: asyncio.Task[None]) -> None:
        if self._refresh_task is task:
            self._refresh_task = None
            self._refresh_generation_id = None
        self._retired_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "MCP health refresh failed (%s)",
                type(exception).__name__,
            )

    async def _discard_snapshot_for_generation_change(self) -> None:
        async with self._lock:
            self._snapshot = None
            self._checked_monotonic = None
            tasks = set(self._retired_tasks)
            if self._refresh_task is not None:
                tasks.add(self._refresh_task)
            self._refresh_task = None
            self._refresh_generation_id = None
            self._retired_tasks.update(tasks)
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_refresh_task(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = set(self._retired_tasks)
            if self._refresh_task is not None:
                tasks.add(self._refresh_task)
            self._refresh_task = None
            self._refresh_generation_id = None
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._retired_tasks.clear()


__all__ = ["MCPHealthCache", "MCPHealthSnapshot"]
