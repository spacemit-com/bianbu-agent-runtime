"""Attest direct Worker children for memoryd's existing PID registry.

This Control-side socket accepts no identity fields and executes no commands.
Keeping the connection open owns the registration lifetime, and the broker
renews that registration for as long as the connection lasts: a stdio MCP
server is a long-lived child, and the expiry it copies from its Worker would
otherwise lapse 24 hours after publication. Command Hooks and stdio MCP
servers are both direct Worker children that reach memoryd, so both use this
broker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import time
from collections.abc import Mapping

from bianbu_agent.server.memory_registry import (
    RETRY_SECONDS,
    MemoryRegistration,
    _start_time_ticks,
)

logger = logging.getLogger(__name__)

REGISTRATION_SOCKET_ENV = 'BIANBU_MEMORY_REGISTRATION_SOCKET'


def command_registration_socket(worker_socket: Path) -> Path:
    # Worker socket paths are capped at 100 bytes; this stays below AF_UNIX 108.
    return Path(str(worker_socket) + '.mem')


def registration_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return the memory-identity socket for a managed Worker's child process.

    Only managed Workers have a Control-attested parent identity. The broker
    lives beside the already mounted Worker socket, so no new sandbox path or
    Security environment allowlist entry is needed. Development leases and
    unmanaged runs get nothing and fall back to memoryd's development identity.
    """

    worker_socket = environment.get('BIANBU_WORKER_SOCKET', '')
    lease = environment.get('BIANBU_WORKER_LEASE_ID', '')
    if not worker_socket or lease in {'', 'dev'}:
        return {}
    if not environment.get('BIANBU_AGENT_MEMORY_REGISTRY_DIR'):
        return {}
    return {REGISTRATION_SOCKET_ENV: str(command_registration_socket(Path(worker_socket)))}


def _parent_pid(pid: int) -> int:
    return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[1])


def _cgroup(pid: int) -> str:
    return Path(f'/proc/{pid}/cgroup').read_text()


class HookMemoryRegistrationServer:
    def __init__(self, path: Path, *, worker_pid: int, registration_path: Path):
        self.path = Path(path)
        self.worker_pid = int(worker_pid)
        self.registration_path = Path(registration_path)
        self._worker_start = _start_time_ticks(self.worker_pid)
        self._server = None
        self._tasks: set[asyncio.Task] = set()
        self._registered: set[int] = set()
        self._renewals: dict[int, MemoryRegistration] = {}

    async def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            raise RuntimeError('Hook registration socket already exists')
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path), limit=128)
        self.path.chmod(0o600)

    def _attest(self, pid: int, uid: int, gid: int) -> tuple[dict, int]:
        parent = json.loads(self.registration_path.read_text())
        if _start_time_ticks(self.worker_pid) != self._worker_start:
            raise PermissionError('Worker process changed')
        if parent['pid_start_time_ticks'] != self._worker_start:
            raise PermissionError('Worker registration changed')
        if (uid, gid) != (parent['uid'], parent['gid']):
            raise PermissionError('Hook credential mismatch')
        if pid == self.worker_pid or _parent_pid(pid) != self.worker_pid:
            raise PermissionError('Hook must be a direct Worker child')
        proc = Path(f'/proc/{pid}')
        owner = proc.stat()
        if (owner.st_uid, owner.st_gid) != (uid, gid):
            raise PermissionError('Hook process ownership changed')
        if _cgroup(pid) != _cgroup(self.worker_pid):
            raise PermissionError('Hook cgroup mismatch')
        if not os.path.samefile(proc / 'exe', f'/proc/{self.worker_pid}/exe'):
            raise PermissionError('Hook must use the Runtime interpreter')
        if str(Path(f'/proc/{self.worker_pid}/exe').resolve()) != parent['executable_path']:
            raise PermissionError('Worker executable changed')
        if parent.get('expires_at_ms', 0) <= int(time.time() * 1000):
            raise PermissionError('Worker registration expired')
        start = _start_time_ticks(pid)
        return {
            **parent,
            'pid_start_time_ticks': start,
            'executable_path': str((proc / 'exe').resolve()),
            'agent_instance_id': f'runtime.{pid}.{start}',
        }, start

    def _publish(self, pid: int, payload: dict) -> tuple[Path, int]:
        path = self.registration_path.parent / f'{pid}.json'
        if path.exists() or path.is_symlink():
            raise PermissionError('Hook process already registered')
        fd, name = tempfile.mkstemp(prefix=f'.hook-{pid}-', dir=path.parent)
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, 'w') as stream:
                json.dump(payload, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic no-replace publication: another registration cannot be clobbered.
            os.link(temporary, path)
            return path, path.stat().st_ino
        finally:
            temporary.unlink(missing_ok=True)

    async def _hold(self, path: Path, pid: int, reader: asyncio.StreamReader) -> None:
        """Keep an attested child's registration alive while it stays connected.

        长寿命子进程（stdio MCP Server）只复制父注册的有效期，而父注册由 Supervisor
        每小时续期，子进程不会。没有这里的续期，一个连续运行的 MCP Server 会在发布
        24 小时后失去 memoryd 身份，之后每一次记忆读写都被拒绝，只能靠重启 Runtime
        恢复。续期只改 expires_at_ms：身份、空间与能力仍是发布时那一次认证的结果。
        """

        try:
            renewal: MemoryRegistration | None = MemoryRegistration(path)
        except (OSError, ValueError, KeyError) as exc:
            renewal = None
            logger.warning('Hook registration renewal is unavailable pid=%s: %s', pid, exc)
        if renewal is not None:
            self._renewals[pid] = renewal
        while True:
            if renewal is not None:
                renewal.maintain()
            try:
                # The client sends no further messages. EOF means cancellation,
                # crash or a deliberate disconnect; either way the child is gone.
                await asyncio.wait_for(reader.read(1), timeout=RETRY_SECONDS)
            except TimeoutError:
                continue
            return

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        path, inode, pid = None, None, None
        acknowledged = False
        try:
            sock = writer.get_extra_info('socket')
            pid, uid, gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if await asyncio.wait_for(reader.readline(), timeout=5) != b'register\n':
                raise PermissionError('Invalid Hook registration request')
            if pid in self._registered:
                raise PermissionError('Hook connection already registered')
            payload, start = self._attest(pid, uid, gid)
            path, inode = self._publish(pid, payload)
            self._registered.add(pid)
            # Close the exit/reuse window before acknowledging the registration.
            if _start_time_ticks(pid) != start or _parent_pid(pid) != self.worker_pid:
                raise PermissionError('Hook process changed during registration')
            writer.write(b'ok\n')
            await writer.drain()
            acknowledged = True
            await self._hold(path, pid, reader)
        except (OSError, ValueError, KeyError, TimeoutError):
            if not acknowledged:
                writer.write(b'denied\n')
                try:
                    await writer.drain()
                except (OSError, ConnectionError):
                    pass
        finally:
            renewal = self._renewals.pop(pid, None)
            if renewal is not None:
                # The record's owner verifies it is still the published file
                # before deleting it; a replaced record is never touched.
                try:
                    renewal.close()
                except OSError as exc:
                    logger.warning('Hook registration cleanup failed pid=%s: %s', pid, exc)
            elif path is not None:
                try:
                    current = path.lstat()
                    if current.st_ino == inode and stat.S_ISREG(current.st_mode):
                        path.unlink()
                except FileNotFoundError:
                    pass
            if pid is not None:
                self._registered.discard(pid)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            self._tasks.discard(task)

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        self.path.unlink(missing_ok=True)
