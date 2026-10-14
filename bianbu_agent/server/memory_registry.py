"""Create the trusted Runtime registration consumed by memoryd."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import time

from bianbu_agent import memory_identity


logger = logging.getLogger(__name__)
TTL_SECONDS = 24 * 60 * 60
RENEW_INTERVAL_SECONDS = 60 * 60
RETRY_SECONDS = 60


_CAPABILITIES = (
    "memory.instruction.read", "memory.instruction.write",
    "memory.short_term.read", "memory.short_term.write",
    "memory.long_term.read", "memory.long_term.write", "memory.forget",
    "memory.account.read", "memory.sync", "memory.backup", "memory.restore",
    "memory.audit.status", "memory.audit.read", "memory.audit.write",
    "memory.privacy.read", "memory.privacy.write",
)


def _start_time_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1]
    return int(fields.split()[19])


def _device_id() -> str:
    configured = os.environ.get("BIANBU_AGENT_MEMORY_DEVICE_ID", "").strip()
    if configured:
        return configured
    virtual_prefixes = (
        "docker", "veth", "br-", "virbr", "tun", "tap", "wg", "tailscale",
    )
    for interface in sorted(Path("/sys/class/net").glob("*/address")):
        name = interface.parent.name
        if name == "lo" or name.startswith(virtual_prefixes):
            continue
        value = interface.read_text(encoding="ascii").strip().lower()
        octets = value.split(":")
        if len(octets) != 6:
            continue
        try:
            mac = bytes(int(octet, 16) for octet in octets)
        except ValueError:
            continue
        if len(mac) == 6 and any(mac) and not (mac[0] & 1):
            return ":".join(f"{octet:02x}" for octet in mac)
    return "unknown-device"


def register(*, agent_name: str | None = None, pid: int | None = None) -> Path | None:
    directory = os.environ.get("BIANBU_AGENT_MEMORY_REGISTRY_DIR", "").strip()
    runtime_mode = os.environ.get("BIANBU_RUNTIME_MODE", "development").strip().lower()
    registration_enabled = os.environ.get(
        "BIANBU_AGENT_MEMORY_REGISTRATION", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not directory or (runtime_mode != "production" and not registration_enabled):
        return None
    root = Path(directory)
    if not root.is_dir():
        raise RuntimeError(f"Agent Memory Runtime registry is unavailable: {root}")
    target_pid = int(pid or os.getpid())
    # The tool adapter resolves the identity it sends to memoryd from the same
    # place, so a Run whose context lost a field still addresses this entry
    # instead of a placeholder (see ``engines/langgraph/plugin_tools.py``).
    account = memory_identity.env_account_id()
    agent = str(agent_name or memory_identity.env_agent_name()).strip() or (
        memory_identity.DEFAULT_AGENT_NAME
    )
    agent_id = (
        os.environ.get("COM_BIANBU_AGENT_ID")
        or os.environ.get("BIANBU_WORKER_AGENT_ID")
        or os.environ.get("BIANBU_AGENT_ID")
        or "com.bianbu.agent"
    ).strip() or "com.bianbu.agent"
    device = _device_id()
    proc = Path(f"/proc/{target_pid}")
    try:
        stat = proc.stat()
        uid = stat.st_uid
        gid = stat.st_gid
        executable = str((proc / "exe").resolve())
    except OSError:
        uid = os.getuid()
        gid = os.getgid()
        executable = str(Path("/proc/self/exe").resolve())
    start = _start_time_ticks(target_pid)
    registration = {
        "uid": uid, "gid": gid, "pid_start_time_ticks": start,
        "account_id": account, "device_id": device, "agent_id": agent_id,
        "agent_name": agent, "agent_instance_id": f"runtime.{target_pid}.{start}",
        "memory_space_id": f"{account}.{agent_id}.{device}",
        "capabilities": list(_CAPABILITIES),
        "executable_path": executable,
        "expires_at_ms": int(time.time() * 1000) + TTL_SECONDS * 1000,
    }
    path = root / f"{target_pid}.json"
    temporary = root / f".{target_pid}.json.tmp"
    temporary.write_text(json.dumps(registration, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)
    os.chmod(path, 0o640)
    return path


def unregister(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class MemoryRegistration:
    """Own renewal of one attested registration, never re-grant its identity.

    Calls are serialized by the owner's event loop. Managed Workers use the
    Supervisor health loop; standalone Workers use run(). No background thread
    may publish after close(). A missing/changed record requires re-attestation.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.pid = int(self.path.stem)
        self._payload = json.loads(self.path.read_text())
        self._inode = self.path.lstat().st_ino
        self._closed = False
        self._error = ""
        self._last_success = int(self._payload["expires_at_ms"]) / 1000 - TTL_SECONDS
        self._next_attempt = 0.0
        self._next_monotonic = time.monotonic() + RENEW_INTERVAL_SECONDS

    def _check_record(self) -> None:
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_ino != self._inode:
            raise PermissionError("Memory registration replaced")
        if json.loads(self.path.read_text()) != self._payload:
            raise PermissionError("Memory registration changed")

    def _check_process(self) -> None:
        if _start_time_ticks(self.pid) != self._payload["pid_start_time_ticks"]:
            raise PermissionError("Memory registration process changed")
        proc = Path(f"/proc/{self.pid}")
        owner = proc.stat()
        if (owner.st_uid, owner.st_gid) != (self._payload["uid"], self._payload["gid"]):
            raise PermissionError("Memory registration process ownership changed")
        if str((proc / "exe").resolve(strict=True)) != self._payload["executable_path"]:
            raise PermissionError("Memory registration executable changed")

    def maintain(self) -> None:
        if self._closed:
            return
        now = time.time()
        if now < self._next_attempt:
            return
        if (not self._error and now < self._last_success + RENEW_INTERVAL_SECONDS
                and time.monotonic() < self._next_monotonic):
            return
        temporary = None
        try:
            self._check_record()
            self._check_process()
            payload = {**self._payload, "expires_at_ms": int(now * 1000) + TTL_SECONDS * 1000}
            fd, name = tempfile.mkstemp(prefix=f".renew-{self.pid}-", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o640)
                json.dump(payload, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            # Do not overwrite a replaced registration or renew a reused PID.
            self._check_record()
            self._check_process()
            os.replace(temporary, self.path)
            self._inode = self.path.lstat().st_ino
            self._payload = payload
            self._last_success = now
            self._next_attempt = 0.0
            self._next_monotonic = time.monotonic() + RENEW_INTERVAL_SECONDS
            if self._error:
                logger.info("Memory registration renewal recovered pid=%s", self.pid)
            self._error = ""
        except (OSError, ValueError, KeyError) as exc:
            self._error = str(exc)
            self._next_attempt = now + RETRY_SECONDS
            logger.error("Memory registration renewal failed pid=%s: %s", self.pid, exc)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Memory renewal temporary cleanup failed pid=%s: %s", self.pid, exc)

    def status(self) -> dict:
        expired = int(self._payload["expires_at_ms"]) <= int(time.time() * 1000)
        state = "stopped" if self._closed else "expired" if expired else "degraded" if self._error else "active"
        return {"state": state, "expires_at_ms": self._payload["expires_at_ms"],
                "last_success_at_ms": int(self._last_success * 1000), "error": self._error}

    async def run(self, stop: asyncio.Event, *, interval: float = RETRY_SECONDS) -> None:
        while not stop.is_set() and not self._closed:
            self.maintain()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._check_record()
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError):
            logger.warning("Memory registration cleanup retained changed record pid=%s", self.pid)
            return
        self.path.unlink()
