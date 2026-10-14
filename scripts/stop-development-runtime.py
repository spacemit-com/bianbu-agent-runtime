#!/usr/bin/env python3
"""Stop the development Runtime that owns one source-checkout data directory."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bianbu_agent.config import RuntimeConfig, load_runtime_config
from bianbu_agent.errors import RuntimeConfigError, RuntimeStartupError
from bianbu_agent.server.process import live_process_record, stop_runtime


def _recorded_config(config: RuntimeConfig) -> RuntimeConfig:
    """Prefer the config that recorded the live instance for this data directory.

    The development launchers share one data directory but start the Runtime with
    different config files (a merged security config, for example), so the PID
    record decides which instance is stopped.  A record from another data
    directory keeps the ownership check in ``stop_runtime``.
    """

    record = live_process_record(config)
    if record is None:
        return config
    try:
        recorded = load_runtime_config(record.config_path)
    except RuntimeConfigError:
        return config
    return recorded if recorded.data_dir == config.data_dir else config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()

    config = load_runtime_config(args.config)
    try:
        stopped = stop_runtime(_recorded_config(config), timeout=args.timeout)
    except RuntimeStartupError as exc:
        print(f"拒绝停止不属于当前数据目录的 Runtime：{exc}", file=sys.stderr)
        return 2
    print(
        "已停止上一 Runtime 实例" if stopped else "没有正在运行的旧 Runtime 实例",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
