"""Shared, board-scoped persistence for directory Kanban tools."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _board_name(board: object) -> str:
    value = str(board or "default").strip()
    return value or "default"


def _board_path(workspace: Path, board: str) -> tuple[Path, Path]:
    digest = hashlib.sha256(board.encode("utf-8")).hexdigest()
    root = Path(workspace) / ".bianbu-agent-state" / "kanban"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.json", root / f"{digest}.lock"


def mutate_kanban(
    workspace: Path,
    board: object,
    callback: Callable[[dict[str, Any]], object],
) -> object:
    """Mutate one board atomically without concealing malformed state."""

    board_name = _board_name(board)
    path, lock_path = _board_path(workspace, board_name)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                state = {"board": board_name, "counter": 0, "tasks": {}}
            except (json.JSONDecodeError, OSError, UnicodeError) as exc:
                return {
                    "success": False,
                    "code": "invalid_state",
                    "error": f"Kanban state is unreadable: {exc}",
                }
            if (
                not isinstance(state, dict)
                or state.get("board") != board_name
                or not isinstance(state.get("tasks"), dict)
            ):
                return {
                    "success": False,
                    "code": "invalid_state",
                    "error": "Kanban state has an invalid structure",
                }
            result = callback(state)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
