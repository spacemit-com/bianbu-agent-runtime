from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    command: str
    timeout_seconds: int = 180
    workdir: str | None = None
    pty: bool = False


@dataclass(frozen=True, slots=True)
class ProcessResult:
    output: str
    exit_code: int
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class ProcessHandle:
    session_id: str
    command: str
