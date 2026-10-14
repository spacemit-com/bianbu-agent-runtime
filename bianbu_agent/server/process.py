"""PID ownership for one configuration-driven Bianbu Agent service."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable, Iterable, Iterator, Protocol

import psutil

from bianbu_agent.config import RuntimeConfig
from bianbu_agent.errors import RuntimeStartupError


_CREATE_TIME_TOLERANCE = 0.01


class _Process(Protocol):
    def create_time(self) -> float: ...
    def cmdline(self) -> list[str]: ...
    def uids(self) -> object: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float) -> object: ...


@dataclass(frozen=True, slots=True)
class RuntimeProcessRecord:
    pid: int
    create_time: float
    config_path: Path


def _tcp_connections() -> Iterable[object]:
    return psutil.net_connections(kind="tcp")


class RuntimeProcessOwner:
    """Acquire and release the PID record for one Runtime data directory."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.path = _record_path(config)
        self._record: RuntimeProcessRecord | None = None

    def acquire(self) -> RuntimeProcessRecord:
        if self.config.config_path is None:
            raise RuntimeStartupError("service startup requires a loaded config path")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        process = psutil.Process(os.getpid())
        record = RuntimeProcessRecord(
            pid=os.getpid(),
            create_time=process.create_time(),
            config_path=Path(self.config.config_path).resolve(),
        )
        with _ownership_lock(self.path):
            existing = _read_or_remove_invalid(self.path)
            if existing is not None:
                if _matching_process(existing) is not None:
                    raise RuntimeStartupError(
                        f"Bianbu Agent is already running with PID {existing.pid}"
                    )
                self.path.unlink(missing_ok=True)
            _write_record_atomically(self.path, record)
            self._record = record
            return record

    def release(self) -> None:
        owned, self._record = self._record, None
        if owned is None:
            return
        with _ownership_lock(self.path):
            current = _read_or_remove_invalid(self.path)
            if current == owned:
                self.path.unlink(missing_ok=True)


def read_process_record(config: RuntimeConfig) -> RuntimeProcessRecord | None:
    """Read the configured Runtime PID record if it exists and is valid."""

    path = _record_path(config)
    with _ownership_lock(path):
        return _read_or_remove_invalid(path)


def live_process_record(config: RuntimeConfig) -> RuntimeProcessRecord | None:
    """Return only a PID record that still identifies its original live process."""

    path = _record_path(config)
    with _ownership_lock(path):
        record = _read_or_remove_invalid(path)
        if record is None:
            return None
        if _matching_process(record) is None:
            path.unlink(missing_ok=True)
            return None
        return record


def stop_runtime(
    config: RuntimeConfig,
    *,
    timeout: float = 3.0,
    process_factory: Callable[[int], _Process] = psutil.Process,
    connections_factory: Callable[[], Iterable[object]] = _tcp_connections,
) -> bool:
    """Stop only the live process whose identity matches the PID record."""

    if timeout <= 0:
        raise ValueError("stop timeout must be positive")
    path = _record_path(config)
    with _ownership_lock(path):
        record = _read_or_remove_invalid(path)
        if record is None:
            process = _configured_listener(
                config,
                process_factory=process_factory,
                connections_factory=connections_factory,
            )
            if process is None:
                return False
        else:
            try:
                process = process_factory(record.pid)
                if abs(float(process.create_time()) - record.create_time) > _CREATE_TIME_TOLERANCE:
                    path.unlink(missing_ok=True)
                    return False
            except psutil.NoSuchProcess:
                path.unlink(missing_ok=True)
                return False
            expected_config = config.config_path
            if expected_config is None or record.config_path != Path(expected_config).resolve():
                raise RuntimeStartupError(
                    f"PID record belongs to a different config: {record.config_path}"
                )
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=1.0)
        except psutil.NoSuchProcess:
            pass
    if record is not None:
        with _ownership_lock(path):
            if _read_or_remove_invalid(path) == record:
                path.unlink(missing_ok=True)
    return True


def _configured_listener(
    config: RuntimeConfig,
    *,
    process_factory: Callable[[int], _Process],
    connections_factory: Callable[[], Iterable[object]],
) -> _Process | None:
    expected_config = config.config_path
    if expected_config is None:
        return None
    listener_pids = {
        pid
        for connection in connections_factory()
        if getattr(connection, "status", None) == psutil.CONN_LISTEN
        and _connection_port(connection) == config.service.port
        and (pid := getattr(connection, "pid", None)) is not None
    }
    matches: list[_Process] = []
    for pid in sorted(listener_pids):
        try:
            process = process_factory(pid)
            if (
                getattr(process.uids(), "real", None) == os.getuid()
                and _command_uses_config(process.cmdline(), Path(expected_config).resolve())
            ):
                matches.append(process)
        except (OSError, TypeError, ValueError, psutil.Error):
            continue
    if len(matches) > 1:
        raise RuntimeStartupError(
            f"multiple Bianbu Agent processes use config {expected_config} on port "
            f"{config.service.port}"
        )
    return matches[0] if matches else None


def _connection_port(connection: object) -> int | None:
    address = getattr(connection, "laddr", None)
    port = getattr(address, "port", None)
    if port is not None:
        return port
    try:
        return address[1]
    except (IndexError, TypeError):
        return None


def _command_uses_config(command: list[str], expected_config: Path) -> bool:
    if not any(Path(argument).name == "bianbu-agent" for argument in command):
        return False
    configured_paths: list[str] = []
    for index, argument in enumerate(command):
        if argument == "--config" and index + 1 < len(command):
            configured_paths.append(command[index + 1])
        elif argument.startswith("--config="):
            configured_paths.append(argument.removeprefix("--config="))
    for configured_path in configured_paths:
        try:
            if Path(configured_path).expanduser().resolve() == expected_config:
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def _record_path(config: RuntimeConfig) -> Path:
    return config.data_dir / "runtime" / "bianbu-agent.pid"


@contextmanager
def _ownership_lock(record_path: Path) -> Iterator[None]:
    directory = record_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeStartupError(f"unsafe Runtime process directory: {directory}")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(directory / "bianbu-agent.lock", flags, 0o600)
    except OSError as exc:
        raise RuntimeStartupError("unsafe Runtime process lock") from exc
    try:
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode):
            raise RuntimeStartupError("unsafe Runtime process lock")
        os.fchmod(descriptor, 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                break
            except InterruptedError:
                continue
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _matching_process(record: RuntimeProcessRecord) -> _Process | None:
    try:
        process = psutil.Process(record.pid)
        if abs(process.create_time() - record.create_time) <= _CREATE_TIME_TOLERANCE:
            return process
    except psutil.NoSuchProcess:
        pass
    return None


def _read_or_remove_invalid(path: Path) -> RuntimeProcessRecord | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("PID record must be a JSON object")
        pid = document["pid"]
        create_time = document["create_time"]
        config_path = document["config_path"]
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("invalid pid")
        if isinstance(create_time, bool) or not isinstance(create_time, (int, float)):
            raise ValueError("invalid create_time")
        if not isinstance(config_path, str) or not config_path:
            raise ValueError("invalid config_path")
        return RuntimeProcessRecord(
            pid=pid,
            create_time=float(create_time),
            config_path=Path(config_path).expanduser().resolve(),
        )
    except FileNotFoundError:
        return None
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        path.unlink(missing_ok=True)
        return None


def _encode_record(record: RuntimeProcessRecord) -> bytes:
    return (
        json.dumps(
            {
                "pid": record.pid,
                "create_time": record.create_time,
                "config_path": str(record.config_path),
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_record_atomically(path: Path, record: RuntimeProcessRecord) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".bianbu-agent-pid-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(_encode_record(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


__all__ = [
    "RuntimeProcessOwner",
    "RuntimeProcessRecord",
    "live_process_record",
    "read_process_record",
    "stop_runtime",
]
