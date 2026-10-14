"""Background lifecycle for a project-local development Runtime."""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import sys
import time
import json
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from bianbu_agent._internal.locking import FilesystemLock
from bianbu_agent.agents import AgentManifest, AgentPackageManager
from bianbu_agent.config import RuntimeConfig, load_runtime_config
from bianbu_agent.home import AgentHome
from bianbu_agent.agents.projects import sync_agent_project
from bianbu_agent.server.process import live_process_record, stop_runtime
from bianbu_agent.observability import RuntimeLogStore


def resolve_project_config(
    project: str | Path,
    explicit: str | Path | None = None,
) -> Path:
    root = Path(project).expanduser().resolve()
    environment = os.environ.get("BIANBU_AGENT_CONFIG")
    if explicit is not None:
        selected = Path(explicit).expanduser().resolve()
        if not selected.is_file():
            raise ValueError(f"explicit Runtime config does not exist: {selected}")
        return selected
    candidates = (
        root / "runtime.yaml",
        Path(environment).expanduser() if environment else None,
    )
    for candidate in candidates:
        if candidate is not None and candidate.resolve().is_file():
            return candidate.resolve()
    raise ValueError("Runtime config not found; use --config or create project runtime.yaml")


def start_development_runtime(
    project: str | Path,
    *,
    config_path: str | Path | None = None,
    timeout: float = 15.0,
) -> dict[str, object]:
    root = Path(project).expanduser().resolve()
    manifest = AgentManifest.from_path(root / "agent.yaml")
    config = load_runtime_config(resolve_project_config(root, config_path))
    state_dir = root / ".bianbu-agent-dev"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FilesystemLock(state_dir / "lifecycle.lock", exclusive=True):
        existing = live_process_record(config)
        if existing is not None:
            raise RuntimeError(
                f"development Runtime is already running with PID {existing.pid}"
            )
        manager = development_package_manager(root, config)
        installed = manager.inspect(manifest.spec.id)
        if installed is None:
            installed = sync_agent_project(root, manager)
        log_file = state_dir / "runtime.log"
        environment = os.environ.copy()
        environment["BIANBU_RUNTIME_MODE"] = "development"
        environment["BIANBU_SECURITY"] = "0"
        environment["BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER"] = "1"
        command = [
            sys.executable,
            "-m",
            "bianbu_agent.cli",
            "--config",
            str(config.config_path),
            "serve",
        ]
        with log_file.open("ab", buffering=0) as log:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
        try:
            _wait_until_ready(config, process, timeout)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise
        record = live_process_record(config)
        return {
            "running": True,
            "pid": record.pid if record is not None else process.pid,
            "agent_id": installed.id,
            "version": installed.version,
            "url": runtime_url(config),
            "log": str(log_file),
        }


def development_status(
    project: str | Path,
    *,
    config_path: str | Path | None = None,
) -> dict[str, object]:
    root = Path(project).expanduser().resolve()
    config = load_runtime_config(resolve_project_config(root, config_path))
    record = live_process_record(config)
    return {
        "running": record is not None,
        "pid": record.pid if record is not None else None,
        "agent_id": AgentManifest.from_path(root / "agent.yaml").spec.id,
        "url": runtime_url(config),
        "log": str(root / ".bianbu-agent-dev" / "runtime.log"),
    }


def uninstall_development_agent(
    agent_id: str,
    project: str | Path,
    *,
    config_path: str | Path | None = None,
) -> dict[str, object]:
    root = Path(project).expanduser().resolve()
    manifest = AgentManifest.from_path(root / "agent.yaml")
    if agent_id != manifest.spec.id:
        raise ValueError("uninstall AGENT_ID must match agent.id")
    config = load_runtime_config(resolve_project_config(root, config_path))
    state_dir = root / ".bianbu-agent-dev"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FilesystemLock(state_dir / "lifecycle.lock", exclusive=True):
        stopped = stop_runtime(config)
        manager = development_package_manager(root, config)
        deleted = manager.uninstall(agent_id)
        return {"stopped": stopped, "deleted": deleted, "agent_id": agent_id}


def development_package_manager(
    project_root: Path, config: RuntimeConfig
) -> AgentPackageManager:
    home = AgentHome(config.data_dir).prepare()
    project_config = project_root / "runtime.yaml"
    private = (
        config.config_path == project_config.resolve()
        and config.data_dir == (project_root / ".bianbu-agent-data").resolve()
        and config.service.title == "Bianbu Agent Development Runtime"
        and (project_root / "agent.yaml").is_file()
    )
    return AgentPackageManager(home)


def log_path(project: str | Path) -> Path:
    return Path(project).expanduser().resolve() / ".bianbu-agent-dev" / "runtime.log"


def tail_development_log(project: str | Path, *, lines: int = 100) -> str:
    if isinstance(lines, bool) or not isinstance(lines, int) or lines <= 0:
        raise ValueError("--lines must be a positive integer")
    path = log_path(project)
    if not path.is_file():
        raise FileNotFoundError(f"development Runtime log does not exist: {path}")
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        return "".join(stream.readlines()[-lines:])


def follow_development_log(project: str | Path, *, lines: int = 100) -> Iterator[str]:
    path = log_path(project)
    initial = tail_development_log(project, lines=lines)
    if initial:
        yield initial
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        stream.seek(0, os.SEEK_END)
        while True:
            chunk = stream.read()
            if chunk:
                yield chunk
            else:
                time.sleep(0.2)


def tail_development_operational_logs(
    project: str | Path,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    lines: int = 100,
    config_path: str | Path | None = None,
) -> str:
    root = Path(project).expanduser().resolve()
    config = load_runtime_config(resolve_project_config(root, config_path))
    store = RuntimeLogStore(config.data_dir / "logs" / "operational" / "runtime.jsonl")
    records = store.query(
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        limit=lines,
    )
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )


def follow_development_operational_logs(
    project: str | Path,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    lines: int = 100,
    config_path: str | Path | None = None,
) -> Iterator[str]:
    seen: set[str] = set()
    while True:
        chunk = tail_development_operational_logs(
            project,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            lines=lines,
            config_path=config_path,
        )
        selected = []
        for line in chunk.splitlines(keepends=True):
            if line not in seen:
                seen.add(line)
                selected.append(line)
        if selected:
            yield "".join(selected)
        time.sleep(0.2)


def runtime_url(config: RuntimeConfig) -> str:
    host = "127.0.0.1" if config.service.host in {"0.0.0.0", "::"} else config.service.host
    return f"http://{host}:{config.service.port}"


def _wait_until_ready(
    config: RuntimeConfig,
    process: subprocess.Popen[bytes],
    timeout: float,
) -> None:
    if timeout <= 0:
        raise ValueError("startup timeout must be positive")
    deadline = time.monotonic() + timeout
    health_url = runtime_url(config) + "/api/health"
    last_error = ""
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"development Runtime exited with status {return_code}")
        try:
            with urlopen(health_url, timeout=min(0.5, timeout)) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(0.05)
    raise TimeoutError(f"development Runtime did not become ready: {last_error}")


__all__ = [
    "development_status",
    "follow_development_log",
    "follow_development_operational_logs",
    "log_path",
    "resolve_project_config",
    "start_development_runtime",
    "tail_development_log",
    "tail_development_operational_logs",
    "uninstall_development_agent",
]
