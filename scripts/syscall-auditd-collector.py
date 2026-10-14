#!/usr/bin/env python3
"""User-space auditd syscall event collector for a single Worker cgroup."""
from __future__ import annotations
import argparse, hashlib, json, os, platform, re, subprocess, time
from pathlib import Path

DENY = {"reboot","swapon","swapoff","kexec_load","kexec_file_load","init_module","finit_module","delete_module","mount","umount2","pivot_root","perf_event_open","ptrace","bpf","userfaultfd"}
SYSCALL = re.compile(r'type=SYSCALL .*?arch=(?P<arch>[^ ]+) syscall=(?P<number>\d+) .*?exit=(?P<exit>-?\d+).*?\bppid=(?P<ppid>\d+) .*?\bpid=(?P<pid>\d+).*?comm="(?P<comm>[^"]+)".*?SYSCALL=(?P<name>[^ ]+)')
STRACE = re.compile(r'^\s*(?:\d+\s+)?(?P<name>[a-zA-Z0-9_]+)\(.*\)\s+=\s+-1\s+(?P<errno>[A-Z0-9]+)')
STRACE_NUMERIC = re.compile(r'^\s*(?:\d+\s+)?syscall_0x(?P<number>[0-9a-fA-F]+)\(.*\)\s+=\s+-1\s+(?P<errno>[A-Z0-9]+)')
ARCH = platform.machine().lower()
if ARCH in {"x86_64", "amd64"}:
    ARCH = "x86_64"
    NUMBERS = {"reboot":169,"swapon":167,"swapoff":168,"kexec_load":246,"kexec_file_load":320,"init_module":175,"finit_module":313,"delete_module":176,"mount":165,"umount2":166,"pivot_root":155,"perf_event_open":298,"ptrace":101,"bpf":321,"userfaultfd":323}
else:
    ARCH = "riscv64" if ARCH in {"riscv64", "risc-v", "risc-v64"} else ARCH
    NUMBERS = {"reboot":142,"swapon":224,"swapoff":225,"kexec_load":104,"kexec_file_load":294,"init_module":105,"finit_module":273,"delete_module":106,"mount":40,"umount2":39,"pivot_root":41,"perf_event_open":241,"ptrace":117,"bpf":280,"userfaultfd":282}

REVERSE_NUMBERS = {number: name for name, number in NUMBERS.items()}

def live_worker_pid(cgroup: str) -> int:
    candidates = []
    for item in Path("/proc").iterdir():
        if not item.name.isdigit(): continue
        try:
            rows = item.joinpath("cgroup").read_text().splitlines()
            actual = next((x.split(":", 2)[2] for x in rows if x.startswith("0::")), "")
            if "/sys/fs/cgroup/" + actual.lstrip("/") == cgroup:
                command = item.joinpath("comm").read_text(errors="replace").strip().lower()
                # Prefer the fixed Worker process.  strace/bwrap/collector
                # are also in the cgroup, but their short-lived descendants
                # can race the securityd identity check.
                score = 0 if any(token in command for token in
                                 ("strace", "bwrap", "syscall-auditd")) else 1
                candidates.append((score, int(item.name)))
        except (OSError, ValueError, StopIteration):
            continue
    if candidates:
        return max(candidates)[1]
    return 0

_TABLES = {
    "c000003e": ("x86_64", dict(zip(
        ("reboot", "swapon", "swapoff", "kexec_load", "kexec_file_load", "init_module", "finit_module", "delete_module", "mount", "umount2", "pivot_root", "perf_event_open", "ptrace", "bpf", "userfaultfd", "openat2"),
        (169, 167, 168, 246, 320, 175, 313, 176, 165, 166, 155, 298, 101, 321, 323, 437)))),
    "c00000f3": ("riscv64", dict(zip(
        ("reboot", "swapon", "swapoff", "kexec_load", "kexec_file_load", "init_module", "finit_module", "delete_module", "mount", "umount2", "pivot_root", "perf_event_open", "ptrace", "bpf", "userfaultfd", "openat2"),
        (142, 224, 225, 104, 294, 105, 273, 106, 40, 39, 41, 241, 117, 280, 282, 437)))),
}


def parse_kernel_event(line: str) -> dict | None:
    """Parse the numeric kernel record, not optional auditd interpretation text."""
    if not line.startswith("type=SECCOMP "):
        return None
    fields = dict(re.findall(r"\b([a-z_]+)=([^\s]+)", line))
    serial = re.search(r"msg=audit\(([0-9.]+:[0-9]+)\)", line)
    try:
        # Kernel audit records the action with its data bits removed.
        if int(fields["code"], 0) & 0xffff0000 != 0x00050000:
            return None
        arch, table = _TABLES[fields["arch"]]
        number, pid = int(fields["syscall"]), int(fields["pid"])
        name = next((name for name, value in table.items() if value == number), None)
        if not serial or not name or pid <= 0:
            return None
    except (KeyError, ValueError):
        return None
    return {"pid": pid, "number": number, "syscall": name, "arch": arch,
            "event_id": "audit-" + serial[1], "backend": "auditd-seccomp"}


def pid_cgroup(pid: int) -> str:
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return "/sys/fs/cgroup/" + line[3:].lstrip("/")
    except OSError:
        pass
    return ""


def source_ready(log: Path, enabled: bool) -> bool:
    if not enabled or not os.access(log, os.R_OK):
        return False
    try:
        if "errno" not in Path("/proc/sys/kernel/seccomp/actions_logged").read_text().split():
            return False
        return subprocess.run(["systemctl", "is-active", "--quiet", "auditd.service"],
                              timeout=2, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def heartbeat(args, log: Path) -> None:
    root = Path("/run/bianbu/syscall-audit")
    name = hashlib.sha256(args.instance_id.encode()).hexdigest() + ".json"
    status = {"pid": os.getpid(), "updated_at": time.time(),
              "instance_id": args.instance_id, "backend": "auditd-seccomp",
              "status": "active" if source_ready(log, args.kernel_seccomp_log) else "unavailable"}
    try:
        tmp = root / (name + ".tmp")
        tmp.write_text(json.dumps(status)); tmp.chmod(0o644); tmp.replace(root / name)
    except OSError:
        # No heartbeat means unavailable, never a fabricated active state.
        pass


def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--cgroup", required=True); p.add_argument("--agent-id", required=True); p.add_argument("--instance-id", required=True); p.add_argument("--lease-id", required=True); p.add_argument("--policy-version", default="0"); p.add_argument("--log", default="/var/log/audit/audit.log"); p.add_argument("--trace-dir", default=""); p.add_argument("--kernel-seccomp-log", action="store_true"); a=p.parse_args()
    cg = a.cgroup
    if not cg.startswith("/sys/fs/cgroup/"):
        cg = "/sys/fs/cgroup/" + cg.lstrip("/")
    f = Path(a.log)
    if not f.exists(): return 78
    trace_offsets: dict[Path, int] = {}
    with f.open("r", errors="replace") as stream:
        stream.seek(0, os.SEEK_END)
        last_heartbeat = 0.0
        while True:
            if time.monotonic() - last_heartbeat >= 2:
                heartbeat(a, f); last_heartbeat = time.monotonic()
            if f.stat().st_ino != os.fstat(stream.fileno()).st_ino:
                stream.close(); stream = f.open("r", errors="replace")
            elif f.stat().st_size < stream.tell():
                stream.seek(0)
            if a.trace_dir:
                for trace in Path(a.trace_dir).glob(".syscall-trace.*"):
                    try:
                        with trace.open("r", errors="replace") as tf:
                            tf.seek(trace_offsets.get(trace, 0))
                            for raw in tf:
                                sm = STRACE.search(raw)
                                # strace on some riscv64 builds prints
                                # syscall_0x111; the generic name regex also
                                # matches that token, so numeric decoding must
                                # take precedence.
                                numeric = STRACE_NUMERIC.search(raw)
                                if numeric:
                                    sm = None
                                if sm:
                                    if sm.group("name") not in DENY or sm.group("errno") != "EPERM": continue
                                    name = sm.group("name")
                                    number = NUMBERS.get(name, -1)
                                elif numeric:
                                    if numeric.group("errno") != "EPERM": continue
                                    number = int(numeric.group("number"), 16)
                                    name = REVERSE_NUMBERS.get(number, "")
                                    if not name: continue
                                else:
                                    continue
                                observed_pid = int(trace.name.rsplit(".", 1)[-1])
                                # The traced terminal child can disappear
                                # between reading strace output and the
                                # securityd lease check. Prefer a still-live
                                # process in the committed Worker cgroup so
                                # securityd can verify /proc/<pid>/cgroup.
                                pid = live_worker_pid(cg)
                                if pid <= 0 and Path(f"/proc/{observed_pid}").exists():
                                    pid = observed_pid
                                if pid <= 0: continue
                                event = {"pid":pid,"syscall":name,"number":number,"arch":ARCH,"backend":"strace-seccomp","agent_id":a.agent_id,"instance_id":a.instance_id,"lease_id":a.lease_id,"policy_version":int(a.policy_version or 0),"event_id":f"strace-{pid}-{name}-{time.time_ns()}"}
                                print(json.dumps(event, ensure_ascii=False), flush=True)
                            trace_offsets[trace] = tf.tell()
                    except (OSError, ValueError):
                        continue
            line = stream.readline()
            if not line: time.sleep(.02); continue
            kernel = parse_kernel_event(line)
            if kernel is not None:
                if pid_cgroup(kernel["pid"]) != cg:
                    continue
                kernel.update(agent_id=a.agent_id, instance_id=a.instance_id,
                              lease_id=a.lease_id, policy_version=int(a.policy_version or 0),
                              cgroup_id=Path(cg).stat().st_ino)
                print(json.dumps(kernel), flush=True)
                continue
            m = SYSCALL.search(line)
            if not m or m.group("name") not in DENY or int(m.group("exit")) != -1: continue
            pid, ppid = int(m.group("pid")), int(m.group("ppid"))
            name, number = m.group("name"), int(m.group("number"))
            # Short-lived shell children may have exited by the time auditd
            # flushes the record.  Validate the child first, then its parent;
            # both must resolve to the collector's Worker cgroup.
            matched = False
            for candidate in (pid, ppid):
                try:
                    actual = next((x.split(":",2)[2] for x in Path(f"/proc/{candidate}/cgroup").read_text().splitlines() if x.startswith("0::")), "")
                    actual = "/sys/fs/cgroup/" + actual.lstrip("/")
                    if actual == cg:
                        matched = True
                        break
                except (OSError, StopIteration):
                    continue
            if not matched: continue
            event = {"pid":pid,"syscall":name,"number":number,"arch":ARCH if m.group("arch") in {"c00000f3", "c000003e"} else m.group("arch"),"backend":"auditd","agent_id":a.agent_id,"instance_id":a.instance_id,"lease_id":a.lease_id,"policy_version":int(a.policy_version or 0),"event_id":f"auditd-{pid}-{number}-{time.time_ns()}"}
            print(json.dumps(event, ensure_ascii=False), flush=True)
    return 0

if __name__ == "__main__": raise SystemExit(main())
