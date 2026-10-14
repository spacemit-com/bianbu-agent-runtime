from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from .records import state_root, is_running

_KEEPER_CODE = """
import os
from pathlib import Path
import sys
import time

descriptor = int(sys.argv[1])
close_path = Path(sys.argv[2])
try:
    while not close_path.exists():
        time.sleep(0.05)
finally:
    os.close(descriptor)
"""


def start(
    command: str,
    workspace: Path,
    workdir: str | None,
    runtime_state_root: Path,
    session_id: str,
    agent_id: str,
) -> dict[str, object]:
    cwd = resolve_workdir(workspace, workdir)
    process_id = f"process-{uuid4().hex[:12]}"
    root = state_root(runtime_state_root)
    output_path = root / f"{process_id}.log"
    metadata_path = root / f"{process_id}.json"
    input_path = root / f"{process_id}.stdin"
    close_path = root / f"{process_id}.close"
    os.mkfifo(input_path, 0o600)
    read_fd = os.open(input_path, os.O_RDONLY | os.O_NONBLOCK)
    keeper_fd = os.open(input_path, os.O_WRONLY | os.O_NONBLOCK)
    flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
    fcntl.fcntl(read_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    output = output_path.open("ab", buffering=0)
    process: subprocess.Popen[bytes] | None = None
    keeper: subprocess.Popen[bytes] | None = None
    try:
        wrapped = (
            f"({command}); __bianbu_exit=$?; "
            f": > {shlex.quote(str(close_path))}; "
            "printf '\n__BIANBU_EXIT__%s\n' \"$__bianbu_exit\""
        )
        process = subprocess.Popen(
            wrapped,
            shell=True,
            cwd=cwd,
            stdin=read_fd,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        keeper = subprocess.Popen(
            [sys.executable, "-c", _KEEPER_CODE, str(keeper_fd), str(close_path)],
            pass_fds=(keeper_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except BaseException:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        input_path.unlink(missing_ok=True)
        close_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(read_fd)
        os.close(keeper_fd)
        output.close()
    assert process is not None and keeper is not None
    metadata = {
        "session_id": process_id,
        "owner_session_id": str(session_id),
        "owner_agent_id": str(agent_id),
        "command": command,
        "pid": process.pid,
        "pid_start_time": _pid_start_time(process.pid),
        "keeper_pid": keeper.pid,
        "output_path": str(output_path),
        "input_path": str(input_path),
        "close_path": str(close_path),
        "started_at": time.time(),
    }
    try:
        _write_json(metadata_path, metadata)
    except BaseException:
        close_path.touch(exist_ok=True)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for child in (process, keeper):
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for path in (input_path, close_path, output_path, metadata_path):
            path.unlink(missing_ok=True)
        raise
    return metadata


def resolve_workdir(workspace: Path, raw: str | None) -> Path:
    workspace = Path(workspace).resolve()
    candidate = workspace if not raw else Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace):
        raise PermissionError("workdir is outside the Tool workspace")
    if not resolved.is_dir():
        raise FileNotFoundError(f"workdir is not a directory: {resolved}")
    return resolved


def output(value: dict[str, object]) -> str:
    path = Path(str(value["output_path"]))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return re.sub(r"\n__BIANBU_EXIT__-?\d+\n?$", "", text)
    except FileNotFoundError:
        return ""


def _pid_start_time(pid: int) -> str:
    try:
        fields = (
            Path(f"/proc/{pid}/stat")
            .read_text(encoding="utf-8")
            .rsplit(")", 1)[1]
            .split()
        )
        return fields[19]
    except OSError, IndexError:
        return ""


def exit_code(value: dict[str, object]) -> int | None:
    try:
        text = Path(str(value["output_path"])).read_text(
            encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return None
    match = re.search(r"\n__BIANBU_EXIT__(-?\d+)\n?$", text)
    return int(match.group(1)) if match else None


def _write_json(path: Path, value: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
