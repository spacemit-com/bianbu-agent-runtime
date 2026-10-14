#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import os
import subprocess
import sys

from bianbu_agent.provisioning import (
    new_agent_id, system_starter_id,
)
from bianbu_agent.config import load_runtime_config


def _data_dir(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Path:
    if (args.config is None) == (args.data_dir is None):
        parser.error("exactly one of --config or --data-dir is required")
    if args.config is not None:
        return load_runtime_config(args.config).data_dir
    return args.data_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--template", type=Path, required=True)
    args = parser.parse_args()
    data_dir = _data_dir(args, parser)
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    agent_id = system_starter_id(data_dir) or new_agent_id()
    command = [
        sys.executable, str(Path(__file__).with_name("provision-system-agent.py")),
        "--data-dir", str(data_dir), "--agent-id", agent_id,
        "--template", str(args.template.resolve()),
    ]
    if os.geteuid() != 0:
        # sudo-rs needs -A to consult SUDO_ASKPASS without a terminal.
        helpers = ["-A"] if os.environ.get("SUDO_ASKPASS") else []
        command = ["sudo", *helpers, "--", *command]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    print(agent_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
