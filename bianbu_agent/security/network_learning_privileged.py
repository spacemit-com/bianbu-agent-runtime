"""Non-privileged client for the root network-learning collector."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


class PrivilegedNetworkLearningClient:
    def __init__(self, *, socket_path: str = "/run/bianbu/network-learning/collector.sock",
                 event_callback: Callable[[Mapping[str, Any]], None] | None = None,
                 status_callback: Callable[[str, str], None] | None = None,
                 timeout: float = 1.0):
        self.socket_path = os.environ.get("BIANBU_NETWORK_LEARNING_SOCKET", socket_path)
        self.event_callback = event_callback
        self.status_callback = status_callback
        self.timeout = max(0.1, float(timeout))
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.instance_id = ""
        self._initial_data = b""

    def _status(self, state: str, reason: str = "") -> None:
        if self.status_callback:
            try: self.status_callback(state, reason)
            except Exception: logger.warning("network collector status callback failed", exc_info=True)

    def register(self, *, instance_id: str, cgroup_path: str, agent_id: str,
                 lease_id: str, pid: int) -> bool:
        self.instance_id = str(instance_id)
        self._registration = {"op": "register", "instance_id": instance_id,
                              "cgroup_path": cgroup_path, "agent_id": agent_id,
                              "lease_id": lease_id, "pid": int(pid)}
        self._stopping.clear()
        connected = self._connect()
        self._thread = threading.Thread(target=self._read_events, daemon=True,
                                       name=f"network-learning-{instance_id}")
        self._thread.start()
        return connected

    def _connect(self) -> bool:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            sock.sendall((json.dumps(self._registration) + "\n").encode())
            chunks = bytearray()
            while b"\n" not in chunks:
                part = sock.recv(4096)
                if not part:
                    raise RuntimeError("collector closed registration connection")
                chunks.extend(part)
            first, pending = bytes(chunks).split(b"\n", 1)
            reply = json.loads(first)
            if not reply.get("ok"):
                raise RuntimeError(str(reply.get("error", "collector rejected registration")))
            # A timeout makes shutdown bounded even if unregister races reconnect.
            sock.settimeout(self.timeout)
            self._socket = sock
            if self._stopping.is_set():
                sock.close()
                self._socket = None
                return False
            self._initial_data = pending
            self._status("active", "")
            return True
        except Exception as exc:
            sock.close()
            self._socket = None
            if not self._stopping.is_set():
                self._status("degraded", f"collector unavailable: {exc}")
            return False

    def _read_events(self) -> None:
        delay = self.timeout
        while not self._stopping.is_set():
            if self._socket is None:
                if self._stopping.wait(delay):
                    break
                if not self._connect():
                    delay = min(delay * 2, 5.0)
                    continue
                delay = self.timeout
            sock = self._socket
            pending, self._initial_data = self._initial_data, b""
            try:
                while not self._stopping.is_set():
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            value = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(value, Mapping) and value.get("event_type") and self.event_callback:
                            try:
                                self.event_callback(value)
                            except Exception:
                                logger.warning("network learning event callback failed", exc_info=True)
                    try:
                        part = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not part:
                        raise ConnectionError("collector closed connection")
                    pending += part
            except OSError as exc:
                if not self._stopping.is_set():
                    self._status("degraded", f"collector connection lost: {exc}")
            finally:
                sock.close()
                if self._socket is sock:
                    self._socket = None

    def unregister(self) -> None:
        self._stopping.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.timeout * 2 + 1)
        self._status("inactive", "unregistered")


__all__ = ["PrivilegedNetworkLearningClient"]
