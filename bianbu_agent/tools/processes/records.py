from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal

_SESSION_ID = re.compile(r"process-[0-9a-f]{12}")


def state_root(runtime_state_root: Path) -> Path:
    root = Path(runtime_state_root).resolve() / "processes"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def records(
    workspace: Path,
    owner_session_id: str,
    owner_agent_id: str,
) -> list[dict[str, object]]:
    result = []
    for path in sorted(state_root(workspace).glob("process-*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue
        if isinstance(value, dict) and _owned(value, owner_session_id, owner_agent_id):
            value["running"] = _matches_process(value)
            result.append(value)
    return result


def record(
    workspace: Path,
    session_id: str,
    owner_session_id: str,
    owner_agent_id: str,
) -> dict[str, object]:
    if _SESSION_ID.fullmatch(session_id) is None:
        raise KeyError(session_id)
    path = state_root(workspace) / f"{session_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KeyError(session_id) from exc
    if not isinstance(value, dict) or not _owned(
        value, owner_session_id, owner_agent_id
    ):
        raise KeyError(session_id)
    value["running"] = _matches_process(value)
    return value


def output(workspace: Path, value: dict[str, object]) -> str:
    path = _stored_path(workspace, value, "output_path", ".log")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return re.sub(r"\n__BIANBU_EXIT__-?\d+\n?$", "", text)
    except FileNotFoundError:
        return ""


def close_stdin(workspace: Path, value: dict[str, object]) -> None:
    path = _stored_path(workspace, value, "close_path", ".close")
    path.touch(exist_ok=True)


def terminate(workspace: Path, value: dict[str, object]) -> None:
    close_stdin(workspace, value)
    pid = int(value.get("pid") or 0)
    if pid > 1 and _matches_process(value):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def write(workspace: Path, value: dict[str, object], data: str) -> None:
    path = _stored_path(workspace, value, "input_path", ".stdin")
    if not path.is_fifo():
        raise BrokenPipeError("process stdin is unavailable")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise BrokenPipeError("process stdin is unavailable") from exc
    try:
        payload = data.encode()
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
    finally:
        os.close(descriptor)


def is_running(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2] != "Z"
    except OSError, IndexError:
        return True


def _matches_process(value: dict[str, object]) -> bool:
    pid = int(value.get("pid") or 0)
    if not is_running(pid):
        return False
    try:
        fields = (
            Path(f"/proc/{pid}/stat")
            .read_text(encoding="utf-8")
            .rsplit(")", 1)[1]
            .split()
        )
        start_time = fields[19]
    except OSError, IndexError:
        return False
    return start_time == str(value.get("pid_start_time") or "")


def exit_code(workspace: Path, value: dict[str, object]) -> int | None:
    try:
        text = _stored_path(workspace, value, "output_path", ".log").read_text(
            encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return None
    match = re.search(r"\n__BIANBU_EXIT__(-?\d+)\n?$", text)
    return int(match.group(1)) if match else None


def _owned(value: dict[str, object], session_id: str, agent_id: str) -> bool:
    return value.get("owner_session_id") == str(session_id) and value.get(
        "owner_agent_id"
    ) == str(agent_id)


def _stored_path(
    workspace: Path,
    value: dict[str, object],
    key: str,
    suffix: str,
) -> Path:
    root = state_root(workspace)
    path = Path(str(value.get(key) or "")).resolve()
    expected_name = f"{value.get('session_id')}{suffix}"
    if path.parent != root or path.name != expected_name:
        raise ValueError(f"invalid process metadata path: {key}")
    return path
