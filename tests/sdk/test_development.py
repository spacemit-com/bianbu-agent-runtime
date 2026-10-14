from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from bianbu_agent.config import load_runtime_config
from bianbu_agent.server.development import (
    development_status,
    resolve_project_config,
    start_development_runtime,
    tail_development_log,
    tail_development_operational_logs,
    uninstall_development_agent,
)
from bianbu_agent.observability import RuntimeLogStore, bind_log_context
import logging
import json
from bianbu_agent.agents.projects import init_agent_project, sync_agent_project
from bianbu_agent.agents import AgentPackageManager
from bianbu_agent.home import AgentHome


def test_config_resolution_prefers_explicit_then_project_then_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text((project / "runtime.yaml").read_text(encoding="utf-8"))
    environment = tmp_path / "environment.yaml"
    environment.write_text((project / "runtime.yaml").read_text(encoding="utf-8"))
    monkeypatch.setenv("BIANBU_AGENT_CONFIG", str(environment))

    assert resolve_project_config(project, explicit) == explicit
    assert resolve_project_config(project) == project / "runtime.yaml"
    (project / "runtime.yaml").unlink()
    assert resolve_project_config(project) == environment


def test_explicit_missing_config_is_not_silently_ignored(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root

    with pytest.raises(ValueError, match="explicit Runtime config does not exist"):
        resolve_project_config(project, tmp_path / "missing.yaml")


def test_tail_log_returns_only_requested_lines(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    log = project / ".bianbu-agent-dev" / "runtime.log"
    log.parent.mkdir(parents=True)
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert tail_development_log(project, lines=2) == "two\nthree\n"


def test_tail_operational_logs_filters_by_run(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    config = load_runtime_config(project / "runtime.yaml")
    store = RuntimeLogStore(config.data_dir / "logs" / "operational" / "runtime.jsonl")
    logger = logging.getLogger("bianbu_agent.test.development_logs")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(store.handler)
    try:
        with bind_log_context(agent_id="demo", run_id="run-a"):
            logger.info("first")
        with bind_log_context(agent_id="demo", run_id="run-b"):
            logger.info("second")
    finally:
        logger.removeHandler(store.handler)

    output = tail_development_operational_logs(project, run_id="run-a", lines=10)

    records = [json.loads(line) for line in output.splitlines()]
    assert [record["message"] for record in records] == ["first"]
    assert records[0]["run_id"] == "run-a"


def test_status_removes_a_stale_pid_record(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    config = load_runtime_config(project / "runtime.yaml")
    record = config.data_dir / "runtime" / "bianbu-agent.pid"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        '{"pid": 999999999, "create_time": 1.0, "config_path": "'
        + str(project / "runtime.yaml")
        + '"}',
        encoding="utf-8",
    )

    status = development_status(project)

    assert status["running"] is False
    assert status["pid"] is None
    assert not record.exists()


def test_uninstall_stops_and_removes_development_agent(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    config = load_runtime_config(project / "runtime.yaml")
    manager = AgentPackageManager(AgentHome(config.data_dir).prepare())
    sync_agent_project(project, manager)

    result = uninstall_development_agent("demo", project)

    assert result == {"stopped": False, "deleted": True, "agent_id": "demo"}
    assert manager.inspect("demo") is None


def test_uninstall_rejects_an_agent_other_than_the_project(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root

    with pytest.raises(ValueError, match="must match agent.id"):
        uninstall_development_agent("other", project)


def test_concurrent_runs_share_one_lifecycle_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bianbu_agent.server import development as development_module

    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    entered = threading.Event()
    release = threading.Event()
    state: dict[str, object] = {"record": None, "sync_calls": 0}

    def live(_config):
        return state["record"]

    def sync(_root, _manager):
        state["sync_calls"] = int(state["sync_calls"]) + 1
        entered.set()
        assert release.wait(timeout=5)
        return SimpleNamespace(id="demo", version="0.1.0")

    class Process:
        pid = 1234

        @staticmethod
        def poll():
            return None

    def ready(_config, _process, _timeout):
        state["record"] = SimpleNamespace(pid=1234)

    monkeypatch.setattr(development_module, "live_process_record", live)
    monkeypatch.setattr(development_module, "sync_agent_project", sync)
    monkeypatch.setattr(development_module.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(development_module, "_wait_until_ready", ready)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(start_development_runtime, project)
        assert entered.wait(timeout=5)
        second = executor.submit(start_development_runtime, project)
        assert state["sync_calls"] == 1
        release.set()
        assert first.result(timeout=5)["pid"] == 1234
        with pytest.raises(RuntimeError, match="already running"):
            second.result(timeout=5)

    assert state["sync_calls"] == 1
