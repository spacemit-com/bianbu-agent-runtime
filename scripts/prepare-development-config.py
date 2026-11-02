#!/usr/bin/env python3
"""Prepare the merged config for a source-checkout Runtime."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bianbu_agent.config import load_runtime_config


def _worker_config_argument(runtime_config: Path, data_dir: Path) -> str:
    """Name the Worker config the way the sandbox namespace sees it."""

    try:
        relative = runtime_config.relative_to(data_dir)
    except ValueError:
        return str(runtime_config)
    return f"/home/agent/{relative.as_posix()}"


def _write_runtime_config(
    base_path: Path, output_path: Path, project_root: Path
) -> Path:
    base = load_runtime_config(base_path)
    document = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("runtime config must contain a mapping")
    document["data_dir"] = str(base.data_dir)
    security = document.setdefault("security", {})
    security["required"] = True
    runtime = document.setdefault("runtime", {})
    control = runtime.setdefault("control", {})
    control["handshake_timeout_seconds"] = max(
        int(control.get("handshake_timeout_seconds", 0)), 60
    )
    worker = runtime.setdefault("worker", {})
    worker["entrypoint"] = [
        sys.executable,
        "-m",
        "bianbu_agent.cli",
        "--role",
        "agent-worker",
        "--config",
        _worker_config_argument(output_path, Path(str(document["data_dir"]))),
        "worker",
    ]
    payload = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, output_path)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--runtime-config-output", required=True, type=Path)
    args = parser.parse_args()

    project_root = args.project_root.expanduser().resolve()
    config_path = args.config.expanduser().resolve()
    runtime_config = _write_runtime_config(
        config_path, args.runtime_config_output.expanduser().resolve(), project_root
    )
    load_runtime_config(runtime_config)
    print(runtime_config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
