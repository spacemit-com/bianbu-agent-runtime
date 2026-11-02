"""Long-lived command Hook processes owned by one Plugin generation.

A command Hook is normally a one-shot process.  On a K3 interpreter start-up
plus the import of the plugin's own modules costs 200-500 ms while the
business call itself is usually a few milliseconds, so that fixed cost lands
on every user-visible request.  A resident process pays it once and then
serves one request per JSON line.

Identity is a generation, not a file.  A Plugin whose content changed produces
a new generation, the command Hook loader copies that generation into a fresh
snapshot directory, and the new snapshot owns a new pool.  Retiring the old
generation stops its pool, so a Hook edited in place can never be served by a
process that still holds the previous code.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time

from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

#: Set for a process that must serve more than one request.
RESIDENT_ENV = "BIANBU_HOOK_RESIDENT"
#: The verified content digest the process was started from, so an operator can
#: attribute a stray process to the generation it belongs to.
DIGEST_ENV = "BIANBU_HOOK_DIGEST"

_LIMIT = 1024 * 1024
_STDERR_TAIL_BYTES = 2048
#: How long a closing stdin may take to end the process before signals start.
_EOF_GRACE_SECONDS = 1.0
_STOP_GRACE_SECONDS = 3.5
#: Concurrent requests per Hook command.  A second process only starts when the
#: first is busy, so an idle device keeps exactly one per Hook.
DEFAULT_POOL_SIZE = 2
DEFAULT_IDLE_SECONDS = 600.0
#: Upper bound on how often an otherwise idle pool looks for expired
#: processes; a short ``idle_seconds`` shortens it so expiry stays prompt.
_REAP_INTERVAL_SECONDS = 30.0


class ResidentHookError(RuntimeError):
    """A resident Hook could not produce a response."""


class ResidentProcess:
    """One resident command Hook process."""

    def __init__(self, command: str, *, root: Path, workspace: Path,
                 data_root: Path, digest: str) -> None:
        self.command = str(command)
        self.root = Path(root)
        self.workspace = Path(workspace)
        self.data_root = Path(data_root)
        self.digest = str(digest)
        self.process: asyncio.subprocess.Process | None = None
        self.stderr_tail = bytearray()
        self._stderr_task: asyncio.Task | None = None
        self.last_used = 0.0

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def _environment(self) -> dict[str, str]:
        from bianbu_agent.hooks.command import hook_environment

        env = hook_environment(
            self.root, workspace=self.workspace, data_root=self.data_root,
        )
        env[RESIDENT_ENV] = "1"
        env[DIGEST_ENV] = self.digest
        return env

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            '/bin/sh', '-c', self.command,
            cwd=self.workspace,
            env=self._environment(),
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_LIMIT + 4096,
        )
        self.last_used = time.monotonic()
        stream = self.process.stderr
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(stream), name=f"hook-stderr-{self.process.pid}",
        )

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """Keep the pipe empty; a full stderr would block the plugin."""

        try:
            while chunk := await stream.read(8192):
                self.stderr_tail.extend(chunk)
                if len(self.stderr_tail) > _STDERR_TAIL_BYTES:
                    del self.stderr_tail[:-_STDERR_TAIL_BYTES]
        except (asyncio.CancelledError, Exception):
            return

    def failure_text(self) -> str:
        tail = bytes(self.stderr_tail).decode('utf-8', 'replace').strip()
        return tail or '(no stderr output)'

    async def call(self, payload: bytes, timeout: float) -> bytes:
        if self.process is None:
            await self.start()
        process = self.process
        assert process is not None
        assert process.stdin is not None and process.stdout is not None
        try:
            async with asyncio.timeout(timeout):
                process.stdin.write(payload)
                await process.stdin.drain()
                line = await process.stdout.readline()
        except TimeoutError:
            await self.kill()
            raise TimeoutError(
                f'Hook command timed out after {timeout:g}s') from None
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self.kill()
            raise ResidentHookError(
                f'Hook command exited unexpectedly: {self.failure_text()}') from exc
        except ValueError as exc:
            # ``readline`` reports a line over the stream limit this way; the
            # stream position is now unusable, exactly like a timeout.
            await self.kill()
            raise ResidentHookError('Hook output exceeds 1 MiB') from exc
        finally:
            self.last_used = time.monotonic()
        if not line:
            await self.kill()
            raise ResidentHookError(
                'Hook command exited with status '
                f'{process.returncode}: {self.failure_text()}')
        return line

    async def kill(self) -> None:
        process, self.process = self.process, None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await self._stderr_task
            self._stderr_task = None
        if process is None:
            return
        # EOF is the protocol's shutdown signal: a resident loop exits on a
        # closed stdin without needing a signal.  Give it a short turn first so
        # Plugin stop stays well inside ``plugin_stop_timeout_seconds``.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=_EOF_GRACE_SECONDS)
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if process.returncode is not None:
                break
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, sig)
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=_STOP_GRACE_SECONDS)
        if process.returncode is None:
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=1.0)


class _Slot:
    def __init__(self, process: ResidentProcess) -> None:
        self.process = process
        self.busy = False


class CommandProcessPool:
    """Resident command Hook processes for one Plugin generation.

    Keyed by ``(plugin root, command, digest)``.  Callers that declare a
    resident Hook share one process; a second process starts only when the
    first is busy, and every process is stopped when the generation retires.
    """

    def __init__(self, *, size: int = DEFAULT_POOL_SIZE,
                 idle_seconds: float = DEFAULT_IDLE_SECONDS) -> None:
        self._size = max(1, int(size))
        self._idle_seconds = float(idle_seconds)
        self._slots: dict[tuple[str, str, str], list[_Slot]] = {}
        self._conditions: dict[tuple[str, str, str], asyncio.Condition] = {}
        self._reaper: asyncio.Task | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _condition(self, key: tuple[str, str, str]) -> asyncio.Condition:
        condition = self._conditions.get(key)
        if condition is None:
            condition = self._conditions[key] = asyncio.Condition()
        return condition

    def _spawn_reaper(self) -> None:
        """Start the idle reaper, which owns expiring unused processes.

        It runs for the life of the pool rather than being started by the next
        caller: a Hook that fires once must still be released, and there may be
        no next call to notice it.
        """

        if self._idle_seconds <= 0 or self._reaper is not None or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reaper = loop.create_task(
            self._reap_forever(), name='hook-pool-reaper')

    async def _reap_forever(self) -> None:
        """Release processes nobody has used for ``idle_seconds``.

        A generation can live for days while a Hook fires once, so idle
        processes must expire without waiting for the next call.  Removal and
        the decision to stop happen under the per-key condition (no caller can
        take that slot in between); the actual kill happens outside it so a
        slow shutdown cannot block a Run that arrives meanwhile.
        """

        interval = max(0.05, min(_REAP_INTERVAL_SECONDS, self._idle_seconds / 4))
        try:
            while True:
                await asyncio.sleep(interval)
                if self._closed:
                    return
                stale: list[ResidentProcess] = []
                for key in tuple(self._slots):
                    condition = self._conditions[key]
                    async with condition:
                        kept: list[_Slot] = []
                        for slot in self._slots.get(key, ()):
                            if (not slot.busy and slot.process.alive
                                    and time.monotonic() - slot.process.last_used
                                    > self._idle_seconds):
                                stale.append(slot.process)
                            else:
                                kept.append(slot)
                        self._slots[key] = kept
                        condition.notify_all()
                if stale:
                    await asyncio.gather(
                        *(process.kill() for process in stale),
                        return_exceptions=True)
        except asyncio.CancelledError:
            return

    async def _acquire(self, key: tuple[str, str, str],
                       spawn: Callable[[], ResidentProcess]) -> _Slot:
        condition = self._condition(key)
        async with condition:
            while True:
                if self._closed:
                    raise ResidentHookError('command Hook pool is closed')
                # Drop a process that died between calls, so a crash is
                # recovered by the next Hook invocation instead of a restart.
                slots = [
                    slot for slot in self._slots.get(key, ())
                    if slot.busy or slot.process.alive
                ]
                self._slots[key] = slots
                self._spawn_reaper()
                for slot in slots:
                    if not slot.busy:
                        slot.busy = True
                        return slot
                if len(slots) < self._size:
                    slot = _Slot(spawn())
                    slots.append(slot)
                    slot.busy = True
                    return slot
                await condition.wait()

    async def _release(self, key: tuple[str, str, str], slot: _Slot) -> None:
        condition = self._condition(key)
        async with condition:
            slot.busy = False
            condition.notify()

    async def call(self, key: tuple[str, str, str],
                   spawn: Callable[[], ResidentProcess],
                   payload: bytes, timeout: float) -> bytes:
        if self._closed:
            raise ResidentHookError('command Hook pool is closed')
        slot = await self._acquire(key, spawn)
        try:
            return await slot.process.call(payload, timeout)
        except BaseException:
            # A timeout, crash or oversized frame leaves the stream in an
            # unknown position, so the process can never be reused.  Cleanup
            # runs in its own task: this task may already be cancelled, and a
            # cancelled await must not leave the process running.
            asyncio.create_task(_discard(slot.process))
            raise
        finally:
            await self._release(key, slot)

    async def prewarm(self, key: tuple[str, str, str],
                      spawn: Callable[[], ResidentProcess]) -> None:
        """Start one idle process without sending a request.

        The Worker calls this while the rest of the generation starts, so the
        first real Run reuses an already running interpreter instead of paying
        process start-up on the user-visible path.  Prewarming is best effort:
        a failure leaves the slot empty and the next call starts normally.
        """

        if self._closed:
            return
        slot = None
        try:
            slot = await self._acquire(key, spawn)
            if not slot.process.alive:
                await slot.process.start()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - prewarming must never fail a plugin
            logger.debug('command Hook prewarm failed key=%s', key, exc_info=True)
        finally:
            if slot is not None:
                await self._release(key, slot)

    async def close(self) -> None:
        self._closed = True
        reaper, self._reaper = self._reaper, None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(BaseException):
                await reaper
        slots = [slot for slots in self._slots.values() for slot in slots]
        self._slots.clear()
        # Wake any caller waiting for a slot so it fails on ``closed`` instead
        # of hanging until the Plugin stop timeout.
        for condition in self._conditions.values():
            async with condition:
                condition.notify_all()
        await asyncio.gather(
            *(slot.process.kill() for slot in slots), return_exceptions=True)


async def _discard(process: ResidentProcess) -> None:
    try:
        await process.kill()
    except BaseException:
        logger.debug('resident Hook cleanup failed', exc_info=True)


__all__ = [
    "CommandProcessPool",
    "DEFAULT_IDLE_SECONDS",
    "DEFAULT_POOL_SIZE",
    "DIGEST_ENV",
    "RESIDENT_ENV",
    "ResidentHookError",
    "ResidentProcess",
]
