#!/usr/bin/env python3
"""Privileged cgroup-scoped network learning collector.

The process is intended to run as root.  Runtime clients register Worker
identity records over a Unix stream socket; events produced by bpftrace are
forwarded only when the event's kernel cgroup belongs to a registered record.
The collector is observational only and reports ``degraded`` when bpftrace is
not available or cannot be started.
"""
import argparse
import dataclasses
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def normalize_cgroup_path(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if not value.startswith("/"):
        value = "/sys/fs/cgroup/" + value.lstrip("/")
    if value != "/sys/fs/cgroup":
        value = value.rstrip("/")
    return value


@dataclasses.dataclass(frozen=True)
class Registration:
    instance_id: str
    cgroup_path: str
    agent_id: str
    lease_id: str
    pid: int
    client: socket.socket | None = dataclasses.field(default=None, compare=False)


class Collector:
    """Protocol state and optional bpftrace process manager.

    ``handle_message`` and ``filter_event`` are deliberately synchronous so
    they can be exercised without root privileges in unit tests.
    """

    def __init__(self, *, socket_path: str = "/run/bianbu/network-learning/collector.sock"):
        self.socket_path = str(socket_path)
        self.registrations: dict[str, Registration] = {}
        self.status = "inactive"
        self.reason = ""
        self._server: socket.socket | None = None
        self._bpftrace: subprocess.Popen[str] | None = None
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._lock = threading.RLock()

    def _set_status(self, status: str, reason: str = "") -> None:
        self.status, self.reason = status, reason

    def register(self, payload: Mapping[str, Any], *, client: socket.socket | None = None) -> Registration:
        required = ("instance_id", "cgroup_path", "agent_id", "lease_id", "pid")
        missing = [key for key in required if str(payload.get(key, "")).strip() == ""]
        if missing:
            raise ValueError("missing registration fields: " + ", ".join(missing))
        cgroup = normalize_cgroup_path(str(payload["cgroup_path"]))
        pid = int(payload["pid"])
        if pid <= 0 or not cgroup:
            raise ValueError("invalid pid or cgroup_path")
        record = Registration(str(payload["instance_id"]), cgroup,
                              str(payload["agent_id"]), str(payload["lease_id"]), pid, client)
        with self._lock:
            self.registrations[record.instance_id] = record
            should_start = self._bpftrace is None
        if should_start and os.environ.get("BIANBU_NETWORK_LEARNING_EBPF", "0") == "1":
            self.start_bpftrace()
        return record

    def unregister(self, instance_id: str) -> bool:
        with self._lock:
            return self.registrations.pop(str(instance_id), None) is not None

    def handle_message(self, message: str | Mapping[str, Any], *, client: socket.socket | None = None) -> dict[str, Any]:
        payload = json.loads(message) if isinstance(message, str) else dict(message)
        op = str(payload.get("op", payload.get("type", ""))).lower()
        if op == "register":
            record = self.register(payload, client=client)
            return {"ok": True, "op": "register", "instance_id": record.instance_id}
        if op == "unregister":
            instance_id = str(payload.get("instance_id", ""))
            if not instance_id:
                raise ValueError("missing instance_id")
            self.unregister(instance_id)
            return {"ok": True, "op": "unregister", "instance_id": instance_id}
        if op == "event":
            event = self.filter_event(payload)
            if event is not None:
                self.emit_event(event)
            return {"ok": True, "op": "event", "accepted": event is not None}
        if op in {"ping", "status"}:
            return {"ok": True, "op": op, "status": self.status, "reason": self.reason}
        raise ValueError(f"unsupported operation: {op or '<empty>'}")

    @staticmethod
    def _pid_cgroup(pid: int) -> str:
        try:
            for line in Path(f"/proc/{int(pid)}/cgroup").read_text().splitlines():
                if line.startswith("0::"):
                    return normalize_cgroup_path(line[3:])
        except (OSError, ValueError):
            pass
        return ""

    def _registration_for_event(self, event: Mapping[str, Any]) -> Registration | None:
        cgroup = normalize_cgroup_path(str(event.get("cgroup_path", "")))
        hinted = str(event.get("instance_id", ""))
        if not cgroup:
            with self._lock:
                if hinted in self.registrations:
                    return self.registrations[hinted]
        if not cgroup and event.get("pid") is not None:
            cgroup = self._pid_cgroup(int(event["pid"]))
        with self._lock:
            for record in self.registrations.values():
                if cgroup and cgroup == record.cgroup_path:
                    return record
        return None

    def filter_event(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        record = self._registration_for_event(event)
        if record is None:
            return None
        normalized = self.normalize_event(event)
        normalized.update({"agent_id": record.agent_id, "instance_id": record.instance_id,
                           "lease_id": record.lease_id, "pid": int(event.get("pid", record.pid))})
        return normalized

    @staticmethod
    def normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
        raw_ip = str(event.get("remote_ip", event.get("ip", ""))).strip()
        try:
            remote_ip = str(ipaddress.ip_address(raw_ip)) if raw_ip else ""
        except ValueError:
            remote_ip = ""
        port = int(event.get("remote_port", event.get("port", 0)) or 0)
        if port < 0 or port > 65535:
            port = 0
        timestamp = event.get("observed_at", event.get("timestamp", time.time()))
        try:
            observed_at = float(timestamp)
        except (TypeError, ValueError):
            observed_at = time.time()
        event_type = str(event.get("event_type", "connect"))
        protocol = str(event.get("protocol", "tcp")).lower()
        errno = str(event.get("errno", "") or "")
        retval = event.get("retval", event.get("result", -1 if event.get("outcome") == "failed" else 0))
        try:
            failed = int(retval) < 0
        except (TypeError, ValueError):
            failed = bool(event.get("outcome") == "failed")
        return {**{key: str(event[key]) for key in ("domain", "dns_qtype") if key in event},
                "observed_at": observed_at, "event_type": event_type,
                "protocol": protocol, "remote_ip": remote_ip,
                "remote_port": port, "outcome": "failed" if failed else "success",
                "errno": errno if failed else "", "backend": str(event.get("backend", "preload"))}

    def parse_bpftrace_line(self, line: str) -> dict[str, Any] | None:
        try:
            value = json.loads(str(line))
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, Mapping) else None

    def start_bpftrace(self, cgroup_path: str | None = None) -> bool:
        binary = shutil.which("bpftrace")
        if not binary:
            self._set_status("degraded", "bpftrace is not installed")
            return False
        program = (
            'tracepoint:sock:inet_sock_set_state '
            '/args->newstate == 1/ '
            '{ printf("{\\"pid\\":%d,\\"timestamp\\":%f,\\"remote_ip\\":\\"%s\\",\\"remote_port\\":%d,\\"protocol\\":\\"tcp\\",\\"retval\\":0}\\n", pid, nsecs/1000000000.0, ntop(2, args->daddr), ntohs(args->dport)); }'
        )
        try:
            self._bpftrace = subprocess.Popen([binary, "-e", program], stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as exc:
            self._set_status("degraded", f"bpftrace start failed: {exc}")
            return False
        self._set_status("active", "")
        thread = threading.Thread(target=self._read_bpftrace, daemon=True, name="network-learning-bpftrace")
        self._threads.append(thread)
        thread.start()
        return True

    def _read_bpftrace(self) -> None:
        proc = self._bpftrace
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                raw = self.parse_bpftrace_line(line)
                if raw is None:
                    continue
                event = self.filter_event({**raw, "backend": "ebpf-tracepoint"})
                if event is not None:
                    self.emit_event(event)
        finally:
            self._bpftrace = None
            if not self._stopping.is_set() and proc.poll() not in (None, 0):
                detail = proc.stderr.read().strip() if proc.stderr is not None else ""
                self._set_status("degraded", detail or "bpftrace exited")

    def emit_event(self, event: Mapping[str, Any]) -> None:
        payload = (json.dumps(dict(event), separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        target = str(event.get("instance_id", ""))
        dead: list[str] = []
        with self._lock:
            for instance_id, registration in self.registrations.items():
                if target and instance_id != target:
                    continue
                if registration.client is None:
                    continue
                try:
                    registration.client.sendall(payload)
                except OSError:
                    dead.append(instance_id)
            for instance_id in dead:
                self.registrations.pop(instance_id, None)

    def stop(self) -> None:
        if self._stopping.is_set():
            self._set_status("inactive", "")
            return
        self._stopping.set()
        proc, self._bpftrace = self._bpftrace, None
        if proc is not None:
            try:
                proc.terminate(); proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass
        server, self._server = self._server, None
        if server is not None:
            try: server.close()
            except OSError: pass
        self._set_status("inactive", "")

    def serve_forever(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try: path.unlink()
        except FileNotFoundError: pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o660)
        self._server.listen(16)
        while not self._stopping.is_set():
            try: client, _ = self._server.accept()
            except OSError:
                break
            thread = threading.Thread(target=self._serve_client, args=(client,), daemon=True)
            self._threads.append(thread); thread.start()

    def _serve_client(self, client: socket.socket) -> None:
        try:
            with client, client.makefile("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        response = self.handle_message(line, client=client)
                    except (ValueError, TypeError, json.JSONDecodeError) as exc:
                        response = {"ok": False, "error": str(exc)}
                    try:
                        client.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
                    except BrokenPipeError:
                        break
                    if response.get("op") == "unregister":
                        self.unregister(str(response.get("instance_id", "")))
        finally:
            with self._lock:
                for instance_id, registration in list(self.registrations.items()):
                    if registration.client is client:
                        self.registrations.pop(instance_id, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/run/bianbu/network-learning/collector.sock")
    args = parser.parse_args(argv)
    collector = Collector(socket_path=args.socket)
    try:
        collector.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
