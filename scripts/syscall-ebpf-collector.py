#!/usr/bin/env python3
"""Minimal cgroup-scoped syscall observer for the Runtime MVP.

The observer never enforces policy; seccomp remains the enforcement point. It
uses bpftrace to read raw_syscalls:sys_enter and emits JSON lines consumed by
the Runtime collector wrapper.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

DENYLIST = {
    "reboot": 169, "swapon": 167, "swapoff": 168,
    "kexec_load": 246, "kexec_file_load": 320,
    "init_module": 175, "finit_module": 313, "delete_module": 176,
    "mount": 165, "umount2": 166, "pivot_root": 155,
    "perf_event_open": 298, "ptrace": 101, "bpf": 321,
    "userfaultfd": 323,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cgroup", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--policy-version", default="0")
    args = parser.parse_args(argv)
    if not os.path.isabs(args.cgroup):
        parser.error("--cgroup must be absolute")
    cgroup_path = args.cgroup
    if not os.path.exists(cgroup_path):
        candidate = os.path.join("/sys/fs/cgroup", args.cgroup.lstrip("/"))
        if os.path.exists(candidate):
            cgroup_path = candidate
    bpftrace = shutil.which("bpftrace")
    if not bpftrace:
        print("bpftrace is not installed", file=sys.stderr)
        return 78
    # cgroupid() accepts a cgroup filesystem path. bpftrace requires elevated
    # BPF permissions; failure is explicit and does not affect seccomp.
    ids = " || ".join(f"args->id == {n}" for n in sorted(DENYLIST.values()))
    program = (
        f'BEGIN {{ @cg = cgroupid("{cgroup_path}"); }} '
        f'raw_syscalls:sys_enter /cgroup == @cg && ({ids})/ '
        '{ printf("%d %d\\n", pid, args->id); }'
    )
    try:
        proc = subprocess.Popen([bpftrace, "-e", program], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 78
    # bpftrace exits immediately when the caller lacks CAP_BPF/root. Surface
    # that as an explicit degraded collector state rather than a false
    # successful observer with zero events.
    import time
    time.sleep(0.05)
    if proc.poll() is not None:
        detail = ""
        if proc.stderr is not None:
            detail = proc.stderr.read().strip()
        if detail:
            print(detail, file=sys.stderr)
        return 78
    reverse = {number: name for name, number in DENYLIST.items()}
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            fields = line.strip().split()
            if len(fields) != 2:
                continue
            try:
                pid, number = (int(value) for value in fields)
            except ValueError:
                continue
            print(json.dumps({"pid": pid, "number": number,
                              "syscall": reverse.get(number, "unknown"),
                              "arch": "x86_64", "backend": "ebpf-tracepoint",
                              "agent_id": args.agent_id, "instance_id": args.instance_id,
                              "lease_id": args.lease_id,
                              "policy_version": int(args.policy_version or 0)},
                             ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        proc.wait(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
