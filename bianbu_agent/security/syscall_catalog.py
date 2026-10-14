"""Architecture-aware syscall metadata used by Runtime explanations."""
from __future__ import annotations

import platform

_DENY = (
    "reboot", "swapon", "swapoff", "kexec_load", "kexec_file_load",
    "init_module", "finit_module", "delete_module", "mount", "umount2",
    "pivot_root", "perf_event_open", "ptrace", "bpf", "userfaultfd",
)

_NUMBERS = {
    "x86_64": {"reboot": 169, "swapon": 167, "swapoff": 168, "kexec_load": 246,
               "kexec_file_load": 320, "init_module": 175, "finit_module": 313,
               "delete_module": 176, "mount": 165, "umount2": 166, "pivot_root": 155,
               "perf_event_open": 298, "ptrace": 101, "bpf": 321, "userfaultfd": 323},
    "riscv64": {"reboot": 142, "swapon": 224, "swapoff": 225, "kexec_load": 104,
                "kexec_file_load": 294, "init_module": 105, "finit_module": 273,
                "delete_module": 106, "mount": 40, "umount2": 39, "pivot_root": 41,
                "perf_event_open": 241, "ptrace": 117, "bpf": 280, "userfaultfd": 282},
}


def normalize_arch(value: str | None = None) -> str:
    value = (value or platform.machine()).strip().lower()
    aliases = {"amd64": "x86_64", "x64": "x86_64", "risc-v": "riscv64", "risc-v64": "riscv64"}
    value = aliases.get(value, value)
    return value if value in _NUMBERS else "unknown"


def syscall_context(arch: str | None = None) -> str:
    resolved = normalize_arch(arch)
    if resolved == "unknown":
        return (f"Worker kernel architecture: {arch or platform.machine()} (unsupported catalog). "
                "Never infer a syscall name or number from another architecture; report the number only.")
    rows = ", ".join(f"{name}={_NUMBERS[resolved][name]}" for name in _DENY)
    return (f"Worker kernel architecture: {resolved}. Syscall numbers are architecture-specific. "
            f"Security denylist catalog for this Worker: {rows}. "
            "When explaining a syscall result, use this catalog and the observed syscall number. "
            "Do not label a number with an x86_64 name when the Worker is riscv64 (or vice versa). "
            "EPERM alone is not proof of seccomp: distinguish seccomp audit evidence from capability, "
            "LSM, or kernel policy denial; state uncertainty when the evidence is unavailable.")
