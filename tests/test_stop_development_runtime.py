from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import psutil


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "stop-development-runtime.py"


def _config(tmp_path: Path, name: str, data_dir: Path) -> Path:
    path = tmp_path / name
    path.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")
    return path


def _sleep_process() -> psutil.Process:
    """Start a detached sleep process so its parent never owns the zombie."""

    script = (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    null = os.open(os.devnull, os.O_RDWR)\n"
        "    os.dup2(null, 0)\n"
        "    os.dup2(null, 1)\n"
        "    os.dup2(null, 2)\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "print(pid, flush=True)\n"
    )
    started = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=True,
    )
    return psutil.Process(int(started.stdout.strip()))


def _record(data_dir: Path, config: Path, process: psutil.Process) -> Path:
    path = data_dir / "runtime" / "bianbu-agent.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "create_time": process.create_time(),
                "config_path": str(config.resolve()),
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(config: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(config), "--timeout", "3"],
        text=True,
        capture_output=True,
        check=False,
    )


def _terminated(process: psutil.Process, limit: float = 5.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not process.is_running():
            return True
        time.sleep(0.1)
    return False


def test_stops_recorded_runtime_for_the_selected_config(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config = _config(tmp_path, "runtime.yaml", data_dir)
    process = _sleep_process()
    try:
        record = _record(data_dir, config, process)

        result = _run(config)

        assert result.returncode == 0, result.stderr
        assert _terminated(process)
        assert not record.exists()
    finally:
        if process.is_running():
            process.kill()


def test_reports_when_no_runtime_is_recorded(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config = _config(tmp_path, "runtime.yaml", data_dir)
    data_dir.mkdir()

    result = _run(config)

    assert result.returncode == 0, result.stderr
    assert "没有正在运行" in result.stderr


def test_stops_runtime_recorded_by_another_config_of_the_same_data_dir(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    launcher_config = _config(tmp_path, "development.yaml", data_dir)
    merged_config = _config(tmp_path, "config-all.yaml", data_dir)
    process = _sleep_process()
    try:
        _record(data_dir, merged_config, process)

        result = _run(launcher_config)

        assert result.returncode == 0, result.stderr
        assert _terminated(process)
    finally:
        if process.is_running():
            process.kill()


def test_rejects_record_from_another_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    other_dir = tmp_path / "other"
    config = _config(tmp_path, "runtime.yaml", data_dir)
    foreign = _config(tmp_path, "foreign.yaml", other_dir)
    process = _sleep_process()
    try:
        record = _record(data_dir, foreign, process)

        result = _run(config)

        assert result.returncode != 0
        assert "different config" in result.stderr
        assert process.is_running()
        assert record.exists()
    finally:
        if process.is_running():
            process.kill()
