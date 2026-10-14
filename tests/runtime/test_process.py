from __future__ import annotations

import json
import importlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import threading
from concurrent.futures import ThreadPoolExecutor

import psutil
import pytest

from bianbu_agent import RuntimeConfig, RuntimeStartupError


runtime_process = (
    importlib.import_module("bianbu_agent.server.process")
    if importlib.util.find_spec("bianbu_agent.server.process") is not None
    else None
)


def test_runtime_process_api_exists() -> None:
    assert runtime_process is not None


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        config_path=tmp_path / "config.yaml",
        data_dir=tmp_path / "data",
    )


def _record(config: RuntimeConfig, *, pid: int, create_time: float) -> Path:
    path = config.data_dir / "runtime" / "bianbu-agent.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "create_time": create_time,
                "config_path": str(config.config_path),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_process_owner_acquires_once_and_releases_only_its_record(tmp_path: Path) -> None:
    config = _config(tmp_path)
    owner = runtime_process.RuntimeProcessOwner(config)

    owner.acquire()

    record = runtime_process.read_process_record(config)
    assert record is not None
    assert record.pid > 0
    assert record.config_path == config.config_path
    with pytest.raises(RuntimeStartupError, match="already running"):
        runtime_process.RuntimeProcessOwner(config).acquire()
    owner.release()
    assert runtime_process.read_process_record(config) is None


def test_process_owner_serializes_simultaneous_starters(tmp_path: Path) -> None:
    for attempt in range(50):
        config = _config(tmp_path / str(attempt))
        barrier = threading.Barrier(2)

        def acquire() -> bool:
            owner = runtime_process.RuntimeProcessOwner(config)
            barrier.wait(timeout=5)
            try:
                owner.acquire()
            except RuntimeStartupError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            acquired = list(executor.map(lambda _index: acquire(), range(2)))

        assert acquired.count(True) == 1


def test_stop_runtime_removes_stale_record_without_signaling(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _record(config, pid=43210, create_time=12.5)

    def missing_process(pid: int):
        raise psutil.NoSuchProcess(pid)

    assert runtime_process.stop_runtime(config, process_factory=missing_process) is False
    assert not path.exists()


def test_stop_runtime_discovers_configured_listener_without_pid_record(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    events: list[object] = []

    class Process:
        def cmdline(self) -> list[str]:
            return [
                "/runtime/.venv/bin/python3",
                "/runtime/.venv/bin/bianbu-agent",
                "--config",
                str(config.config_path),
            ]

        def uids(self):
            return SimpleNamespace(real=os.getuid())

        def terminate(self) -> None:
            events.append("terminate")

        def wait(self, timeout: float) -> None:
            events.append(("wait", timeout))

    listener = SimpleNamespace(
        status=psutil.CONN_LISTEN,
        laddr=SimpleNamespace(port=config.service.port),
        pid=43210,
    )

    assert runtime_process.stop_runtime(
        config,
        timeout=3.0,
        process_factory=lambda pid: Process() if pid == 43210 else None,
        connections_factory=lambda: [listener],
    ) is True
    assert events == ["terminate", ("wait", 3.0)]


def test_stop_runtime_does_not_signal_foreign_listener_without_pid_record(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    class Process:
        def cmdline(self) -> list[str]:
            return ["python", "other-server", "--config", str(config.config_path)]

        def uids(self):
            return SimpleNamespace(real=os.getuid())

        def terminate(self) -> None:
            raise AssertionError("foreign listener must not be signaled")

    listener = SimpleNamespace(
        status=psutil.CONN_LISTEN,
        laddr=SimpleNamespace(port=config.service.port),
        pid=43210,
    )

    assert runtime_process.stop_runtime(
        config,
        process_factory=lambda pid: Process() if pid == 43210 else None,
        connections_factory=lambda: [listener],
    ) is False


def test_stop_runtime_removes_stale_record_before_config_ownership_check(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    path = _record(config, pid=43210, create_time=12.5)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["config_path"] = str(tmp_path / "legacy-home" / "config.yaml")
    path.write_text(json.dumps(document), encoding="utf-8")

    def missing_process(pid: int):
        raise psutil.NoSuchProcess(pid)

    assert runtime_process.stop_runtime(config, process_factory=missing_process) is False
    assert not path.exists()


@pytest.mark.parametrize("document", ["null", "[]", '"pid"'])
def test_process_record_removes_non_mapping_json(
    tmp_path: Path,
    document: str,
) -> None:
    config = _config(tmp_path)
    path = config.data_dir / "runtime" / "bianbu-agent.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")

    assert runtime_process.read_process_record(config) is None
    assert not path.exists()


def test_stop_runtime_does_not_signal_reused_pid(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _record(config, pid=43210, create_time=12.5)

    class ReusedProcess:
        def create_time(self) -> float:
            return 99.0

        def terminate(self) -> None:
            raise AssertionError("reused PID must not be signaled")

    assert runtime_process.stop_runtime(
        config, process_factory=lambda _pid: ReusedProcess()
    ) is False
    assert not path.exists()


def test_stop_runtime_terminates_matching_owner(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _record(config, pid=43210, create_time=12.5)
    events: list[object] = []

    class Process:
        def create_time(self) -> float:
            return 12.5

        def terminate(self) -> None:
            events.append("terminate")

        def wait(self, timeout: float) -> None:
            events.append(("wait", timeout))

    assert runtime_process.stop_runtime(
        config, timeout=3.0, process_factory=lambda _pid: Process()
    ) is True
    assert events == ["terminate", ("wait", 3.0)]
    assert not path.exists()


def test_stop_runtime_rejects_live_owner_from_another_config(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _record(config, pid=43210, create_time=12.5)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["config_path"] = str(tmp_path / "foreign" / "config.yaml")
    path.write_text(json.dumps(document), encoding="utf-8")

    class Process:
        def create_time(self) -> float:
            return 12.5

        def terminate(self) -> None:
            raise AssertionError("foreign owner must not be signaled")

    with pytest.raises(RuntimeStartupError, match="different config"):
        runtime_process.stop_runtime(config, process_factory=lambda _pid: Process())

    assert path.exists()


def test_stop_runtime_kills_owner_after_timeout(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _record(config, pid=43210, create_time=12.5)
    events: list[object] = []

    class Process:
        def create_time(self) -> float:
            return 12.5

        def terminate(self) -> None:
            events.append("terminate")

        def kill(self) -> None:
            events.append("kill")

        def wait(self, timeout: float) -> None:
            events.append(("wait", timeout))
            if len([item for item in events if isinstance(item, tuple)]) == 1:
                raise psutil.TimeoutExpired(timeout)

    assert runtime_process.stop_runtime(
        config, timeout=3.0, process_factory=lambda _pid: Process()
    ) is True
    assert events == ["terminate", ("wait", 3.0), "kill", ("wait", 1.0)]
