"""Best-effort syscall event collector for managed Workers.

The kernel filter remains the enforcement point.  This module only manages an
optional observer process (for example an eBPF/audit collector) and scopes it
to the Worker cgroup.  If no collector binary is configured the manager is a
safe no-op; seccomp enforcement is never weakened.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


class SyscallCollector:
    """Lifecycle wrapper around an external cgroup-scoped collector.

    The collector must emit one JSON object per line on stdout.  Runtime does
    not trust identity fields from that stream: callers should forward events
    to securityd, which performs lease/cgroup validation before persistence.
    """

    def __init__(self, *, command: str | None = None,
                 popen_factory=subprocess.Popen,
                 event_callback: Callable[[Mapping[str, Any]], None] | None = None):
        configured = command if command is not None else os.environ.get("BIANBU_SYSCALL_COLLECTOR_CMD", "")
        if command is None and not configured and os.path.isfile("/usr/lib/bianbu-agent-runtime/syscall-auditd-collector.py") and os.access("/var/log/audit/audit.log", os.R_OK):
            configured = "/usr/bin/python3 /usr/lib/bianbu-agent-runtime/syscall-auditd-collector.py"
        self.command = configured.strip()
        self._popen = popen_factory
        self._callback = event_callback
        self.process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    def start(self, *, cgroup_path: str, agent_id: str, instance_id: str,
              lease_id: str, policy_version: str | int = "", trace_dir: str = "", kernel_seccomp_log: bool = False) -> bool:
        if not self.enabled:
            return False
        if not cgroup_path:
            raise ValueError("collector requires a cgroup path")
        # /proc/<pid>/cgroup returns a v2 path relative to the mounted
        # hierarchy; normalize it for collectors such as bpftrace.
        if not cgroup_path.startswith("/"):
            cgroup_path = "/sys/fs/cgroup/" + cgroup_path.lstrip("/")
        argv = shlex.split(self.command)
        if not argv:
            return False
        argv += ["--cgroup", cgroup_path, "--agent-id", agent_id,
                 "--instance-id", instance_id, "--lease-id", lease_id,
                 "--policy-version", str(policy_version)]
        if kernel_seccomp_log:
            argv += ["--kernel-seccomp-log"]
        if trace_dir:
            argv += ["--trace-dir", trace_dir]
        try:
            self.process = self._popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            logger.warning("syscall collector unavailable (optional): %s", exc)
            self.process = None
            return False
        self._thread = threading.Thread(target=self._read_events, daemon=True,
                                        name=f"syscall-collector-{instance_id}")
        self._thread.start()
        return True

    def _read_events(self) -> None:
        proc = self.process
        stream = getattr(proc, "stdout", None)
        if stream is None:
            return
        try:
            for line in stream:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    logger.debug("ignore malformed syscall collector event")
                    continue
                if isinstance(event, Mapping) and self._callback is not None:
                    try:
                        self._callback(event)
                    except Exception:
                        logger.exception("syscall collector event callback failed")
        except Exception:
            logger.debug("syscall collector stream closed", exc_info=True)

    def stop(self, timeout: float = 1.0) -> None:
        proc, self.process = self.process, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=max(0.1, timeout))
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


__all__ = ["SyscallCollector"]
