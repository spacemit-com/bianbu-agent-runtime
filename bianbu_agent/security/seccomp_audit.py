"""Request kernel audit logging without changing an existing seccomp policy.

The bwrap filter is already installed. Stacking the same filter with LOG
retains its ERRNO action and inheritance, and avoids ptrace Worker stops.
"""
from __future__ import annotations
import ctypes
import platform
from pathlib import Path


class _Filter(ctypes.Structure):
    _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]


class _Program(ctypes.Structure):
    _fields_ = [('length', ctypes.c_ushort), ('filter', ctypes.POINTER(_Filter))]


def enable_filter_logging(path: Path, *, libc=None, arch: str | None = None) -> None:
    raw = Path(path).read_bytes()
    if not raw or len(raw) % 8 or len(raw) > 4096 * 8:
        raise ValueError('invalid seccomp filter size')
    number = {'x86_64': 317, 'riscv64': 277}.get(arch or platform.machine())
    if number is None:
        raise ValueError('unsupported seccomp audit architecture')
    filters = (_Filter * (len(raw) // 8)).from_buffer_copy(raw)
    program = _Program(len(filters), filters)
    libc = libc or ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), 'cannot set no_new_privs')
    # SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_LOG. A failure must not
    # silently advertise a Worker whose required audit source is absent.
    if libc.syscall(number, 1, 2, ctypes.byref(program)) != 0:
        raise OSError(ctypes.get_errno(), 'cannot enable seccomp kernel logging')
