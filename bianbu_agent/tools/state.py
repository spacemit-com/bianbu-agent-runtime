from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


class ConsoleState:
    """Small atomic document store for console-managed product settings."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.is_file():
                return {}
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("console state must be an object")
            return value

    def section(self, name: str, default: Any) -> Any:
        return deepcopy(self.read().get(name, default))

    def set_section(self, name: str, value: Any) -> None:
        with self._lock:
            state = self.read()
            state[name] = value
            self._write(state)

    def update_section(self, name: str, key: str, value: Any) -> None:
        with self._lock:
            state = self.read()
            section = state.setdefault(name, {})
            if not isinstance(section, dict):
                section = {}
                state[name] = section
            section[key] = value
            self._write(state)

    def delete_section_item(self, name: str, key: str) -> bool:
        with self._lock:
            state = self.read()
            section = state.get(name)
            if not isinstance(section, dict) or key not in section:
                return False
            del section[key]
            self._write(state)
            return True

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def confined_path(root: Path, relative: str, *, suffix: str | None = None) -> Path:
    """Resolve a user path below root without permitting symlink escape."""

    if "\x00" in relative:
        raise ValueError("invalid path")
    candidate = (root / relative.lstrip("/")).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError("path escapes root")
    if suffix is not None and candidate.suffix.lower() != suffix.lower():
        raise ValueError(f"path must end in {suffix}")
    return candidate


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]
