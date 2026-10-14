"""Parse per-Worker strace network observations and manage the tail process."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

_CALL = re.compile(
    r"^\s*(?:(?P<timestamp>\d+(?:\.\d+)?)\s+)?"
    r"(?P<call>connect|sendto|sendmsg)\("
)
_PORT = re.compile(r"\bsin6?_port=htons\((?P<port>\d+)\)")
_IPV4 = re.compile(r'inet_addr\("(?P<ip>[^"\s]+)"\)')
_IPV6 = re.compile(r'inet_pton\(AF_INET6,\s*"(?P<ip>[^"]+)"')
_RESULT = re.compile(
    r"\)\s+=\s+(?P<result>-?\d+)(?:\s+(?P<errno>[A-Z][A-Z0-9_]+))?"
    r"(?:\s|$)"
)
_SENDTO_PAYLOAD = re.compile(r'sendto\([^,]+,\s*"(?P<payload>(?:\\.|[^"])*)"')
_SENDMSG_PAYLOAD = re.compile(r'iov_base="(?P<payload>(?:\\.|[^"])*)"')

_DNS_TYPES = {
    1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX",
    16: "TXT", 28: "AAAA", 33: "SRV", 65: "HTTPS",
}


def _decode_strace_bytes(value: str) -> bytes:
    output = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\":
            code = ord(char)
            if code > 255:
                return b""
            output.append(code)
            index += 1
            continue
        index += 1
        if index >= len(value):
            return b""
        escape = value[index]
        if escape == "x" and index + 2 < len(value):
            try:
                output.append(int(value[index + 1:index + 3], 16))
            except ValueError:
                return b""
            index += 3
            continue
        if escape in "01234567":
            end = index + 1
            while end < min(index + 3, len(value)) and value[end] in "01234567":
                end += 1
            output.append(int(value[index:end], 8))
            index = end
            continue
        replacements = {"n": 10, "r": 13, "t": 9, "\\": 92, '"': 34}
        output.append(replacements.get(escape, ord(escape) & 0xFF))
        index += 1
    return bytes(output)


def _dns_question(packet: bytes) -> tuple[str, str] | None:
    if len(packet) < 17 or int.from_bytes(packet[4:6], "big") < 1:
        return None
    labels: list[str] = []
    offset = 12
    while offset < len(packet):
        length = packet[offset]
        offset += 1
        if length == 0:
            break
        if length > 63 or length & 0xC0 or offset + length > len(packet):
            return None
        try:
            label = packet[offset:offset + length].decode("ascii")
        except UnicodeDecodeError:
            return None
        if not label or any(char.isspace() for char in label):
            return None
        labels.append(label.lower())
        offset += length
    if not labels or offset + 4 > len(packet):
        return None
    qtype = int.from_bytes(packet[offset:offset + 2], "big")
    return ".".join(labels), _DNS_TYPES.get(qtype, f"TYPE{qtype}")


def parse_strace_line(line: str) -> list[dict[str, Any]]:
    """Return independent connect and DNS facts from one complete strace line."""
    selected = str(line).strip()
    call_match = _CALL.search(selected)
    result_match = _RESULT.search(selected)
    port_match = _PORT.search(selected)
    ip_match = _IPV6.search(selected) or _IPV4.search(selected)
    if not call_match or not result_match or not port_match or not ip_match:
        return []
    try:
        raw_ip = ip_match.group("ip")
        if "\\" in raw_ip:
            raw_ip = _decode_strace_bytes(raw_ip).decode("ascii")
        remote_ip = str(ipaddress.ip_address(raw_ip))
        remote_port = int(port_match.group("port"))
        observed_at = float(call_match.group("timestamp") or time.time())
        result = int(result_match.group("result"))
    except (TypeError, ValueError):
        return []
    if remote_port < 1 or remote_port > 65535:
        return []
    call = call_match.group("call")
    errno = str(result_match.group("errno") or "")
    failed = result < 0 and not (call == "connect" and errno == "EINPROGRESS")
    outcome = "failed" if failed else "success"
    error = errno if failed else ""
    protocol = "tcp" if call == "connect" else "udp"
    events: list[dict[str, Any]] = [{
        "observed_at": observed_at,
        "event_type": "connect",
        "protocol": protocol,
        "remote_ip": remote_ip,
        "remote_port": remote_port,
        "outcome": outcome,
        "errno": error,
    }]
    if call in {"sendto", "sendmsg"} and remote_port == 53:
        payload_match = (_SENDTO_PAYLOAD.search(selected) if call == "sendto"
                         else _SENDMSG_PAYLOAD.search(selected))
        question = (_dns_question(_decode_strace_bytes(payload_match.group("payload")))
                    if payload_match else None)
        if question is not None:
            events.append({
                "observed_at": observed_at,
                "event_type": "dns_query",
                "protocol": "udp",
                "domain": question[0],
                "remote_ip": remote_ip,
                "remote_port": remote_port,
                "dns_qtype": question[1],
                "outcome": outcome,
                "errno": error,
            })
    return events


class NetworkLearningCollector:
    """Read structured observations from a per-instance trace tail process."""

    def __init__(self, *, command: str | None = None,
                 popen_factory=subprocess.Popen,
                 event_callback: Callable[[Mapping[str, Any]], None] | None = None,
                 status_callback: Callable[[str, str], None] | None = None):
        if command is None:
            configured = os.environ.get("BIANBU_NETWORK_LEARNING_COLLECTOR_CMD", "")
            packaged = Path("/usr/lib/bianbu-agent-runtime/network-learning-collector.py")
            if not configured and packaged.is_file():
                configured = f"{shlex.quote(sys.executable)} {shlex.quote(str(packaged))}"
            command = configured
        self.command = str(command or "").strip()
        self._popen = popen_factory
        self._event_callback = event_callback
        self._status_callback = status_callback
        self._stopping = False
        self.process: subprocess.Popen[str] | None = None
        self.trace_process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._trace_thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    def _status(self, state: str, reason: str = "") -> None:
        if self._status_callback is None:
            return
        try:
            self._status_callback(state, reason)
        except Exception:
            logger.warning("network learning status callback failed", exc_info=True)

    def start(self, *, trace_dir: str, attach_pid: int | None = None) -> bool:
        if not self.enabled or not str(trace_dir).strip():
            return False
        trace_path = Path(trace_dir)
        if attach_pid is not None:
            try:
                trace_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._status("degraded", f"trace directory unavailable: {exc}")
                return False
            strace = shutil.which("strace")
            if not strace or int(attach_pid) <= 0:
                self._status("degraded", "strace attach unavailable")
                return False
            trace_argv = [
                # Attach to the Worker leader only.  Following forked tool
                # processes with ``-ff`` can leave curl/shell children in a
                # ptrace-stop on production devices and block Run cancellation.
                strace, "-qq", "-ttt", "-xx", "-s", "512",
                "-e", "trace=connect,sendto,sendmsg", "-o",
                str(trace_path / ".syscall-trace"), "-p", str(int(attach_pid)),
            ]
            try:
                self.trace_process = self._popen(
                    trace_argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=True, start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                self.trace_process = None
                self._status("degraded", f"strace attach failed: {exc}")
                return False
        argv = [*shlex.split(self.command), "--trace-dir", str(trace_dir)]
        try:
            self.process = self._popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            self.process = None
            self._stop_process(self.trace_process, 1.0)
            self.trace_process = None
            self._status("degraded", f"collector start failed: {exc}")
            return False
        self._stopping = False
        self._status("active", "")
        self._thread = threading.Thread(
            target=self._read_events, daemon=True,
            name="network-learning-collector",
        )
        self._thread.start()
        if self.trace_process is not None:
            self._trace_thread = threading.Thread(
                target=self._watch_trace_process, daemon=True,
                name="network-learning-strace",
            )
            self._trace_thread.start()
        return True

    @staticmethod
    def _stop_process(process: subprocess.Popen[str] | None, timeout: float) -> None:
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=max(0.1, float(timeout)))
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _watch_trace_process(self) -> None:
        process = self.trace_process
        if process is None:
            return
        try:
            process.wait()
        except Exception as exc:
            if not self._stopping:
                self._status("degraded", f"strace monitor failed: {exc}")
            return
        if self._stopping:
            return
        reason = "strace attach exited"
        error_stream = getattr(process, "stderr", None)
        if error_stream is not None:
            try:
                detail = str(error_stream.read()).strip()
                if detail:
                    reason = detail[:1024]
            except Exception:
                pass
        self._status("degraded", reason)

    def _read_events(self) -> None:
        process = self.process
        stream = getattr(process, "stdout", None)
        if stream is None:
            if not self._stopping:
                self._status("degraded", "collector stdout unavailable")
            return
        try:
            for line in stream:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(event, Mapping) and self._event_callback is not None:
                    try:
                        self._event_callback(event)
                    except Exception:
                        logger.warning("network learning event callback failed", exc_info=True)
        except Exception as exc:
            if not self._stopping:
                self._status("degraded", f"collector stream failed: {exc}")
            return
        if self._stopping:
            return
        reason = "collector exited"
        error_stream = getattr(process, "stderr", None)
        if error_stream is not None:
            try:
                detail = str(error_stream.read()).strip()
                if detail:
                    reason = detail[:1024]
            except Exception:
                pass
        self._status("degraded", reason)

    def stop(self, timeout: float = 1.0) -> None:
        self._stopping = True
        process, self.process = self.process, None
        trace_process, self.trace_process = self.trace_process, None
        self._stop_process(process, timeout)
        self._stop_process(trace_process, timeout)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout)))
        trace_thread = self._trace_thread
        if trace_thread is not None and trace_thread is not threading.current_thread():
            trace_thread.join(timeout=max(0.1, float(timeout)))
        self._status("inactive", "stopped")


__all__ = ["NetworkLearningCollector", "parse_strace_line"]
