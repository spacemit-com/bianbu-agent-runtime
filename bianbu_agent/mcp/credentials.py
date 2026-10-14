"""Small, permission-safe credential store for managed MCP servers."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def env_path() -> Path:
    return Path.home() / ".hermes" / ".env"


def read_env(path: Path | None = None) -> dict[str, str]:
    target = path or env_path()
    if not target.is_file():
        return {}
    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def write_secret(key: str, value: str, path: Path | None = None) -> Path:
    target = path or env_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    values = read_env(target)
    values[key] = value
    target.write_text("".join(f"{k}={v}\n" for k, v in sorted(values.items())), encoding="utf-8")
    os.chmod(target, 0o600)
    return target


def load_environment() -> dict[str, str]:
    """Credentials for managed MCP servers, plus this Runtime's interpreter.

    ``RUNTIME_PYTHON`` and ``RUNTIME_PYTHON_BIN_DIR`` describe the process that
    expands the MCP definition, which is also the process that starts the
    child: memoryd authenticates a stdio MCP server by its executable, so the
    Worker and its MCP children must share one interpreter.  Command Hooks get
    ``RUNTIME_PYTHON`` in the environment the Runtime builds for them; an MCP
    definition expands it here instead, because a stdio child environment
    carries only the variables the definition declares.
    """

    values = dict(os.environ)
    values.update(read_env())
    values.setdefault("RUNTIME_PYTHON", sys.executable)
    values.setdefault("RUNTIME_PYTHON_BIN_DIR", str(Path(sys.executable).parent))
    return values


__all__ = ["env_path", "load_environment", "read_env", "write_secret"]
