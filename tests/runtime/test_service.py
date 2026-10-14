from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import yaml

import bianbu_agent
from bianbu_agent import (
    AgentHome,
    AgentModel,
    AgentPackageManager,
    AgentSpec,
    InMemoryAgentRegistry,
)
from bianbu_agent.api.contracts import ExecutionView
from bianbu_agent.errors import AgentValidationError
from bianbu_agent.mcp.managed import ManagedMCPDefinition, write_managed_mcp_package
from bianbu_agent.models import YAMLModelResolver
from bianbu_agent.models import YAMLModelStore, ModelConfig
from bianbu_agent.server.runtime import _bind_security_identity
from bianbu_agent.sessions import SessionInboxMessage
from bianbu_agent.plugins import (
    PluginGeneration,
    PluginRuntime,
    PluginContributions,
    SkillContribution,
    SkillSpec,
)


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "deployment" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "data_dir": "./data",
                "service": {
                    "title": "Configured Runtime",
                    "host": "127.0.0.1",
                    "port": 18765,
                },
                "language": "zh",
                "require_file_backed_capabilities": True,
                "plugin_stop_timeout_seconds": 5.0,
                "allow_unmanaged_capabilities": False,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


async def _prepare_runtime_data(config_path: Path) -> None:
    config = bianbu_agent.load_runtime_config(config_path)
    home = AgentHome(config.data_dir).prepare()
    AgentPackageManager(home).create(
        AgentSpec(
            id="default",
            name="Default",
            model=AgentModel("openai", "test-model"),
        )
    )
    store = YAMLModelStore(home.models_dir)
    await store.open()
    try:
        await store.create(
            ModelConfig(
                id="provider-openai",
                provider="openai",
                model="credential",
                base_url="http://127.0.0.1:1/v1",
                parameters={"record_kind": "provider_credential", "api_key": "test"},
            )
        )
    finally:
        await store.close()


def test_runtime_service_public_api_exists() -> None:
    assert hasattr(bianbu_agent, "BianbuAgentRuntime")
    assert callable(getattr(bianbu_agent, "create_runtime", None))
    assert callable(getattr(bianbu_agent, "create_runtime_app", None))


def test_execution_view_accepts_unit_scope() -> None:
    view = ExecutionView(
        instance_id="i-1",
        agent_id="a-1",
        backend="host",
        pid=123,
        cgroup_path="/user.slice/test",
        unit="bianbu-agent-a-i-1.scope",
        unit_scope="system",
        state="RUNNING",
        lease_id="lease-1",
        started_at=0.0,
        sandboxed=False,
    )
    assert view.unit_scope == "system"


def test_execution_view_defaults_unit_scope_for_legacy_records() -> None:
    payload = {
        "instance_id": "i-1", "agent_id": "a-1", "backend": "host",
        "pid": 123, "cgroup_path": "/user.slice/test",
        "unit": "bianbu-agent-a-i-1.service", "state": "RUNNING",
        "lease_id": "lease-1", "started_at": 0.0, "sandboxed": False,
    }
    assert ExecutionView.model_validate(payload).unit_scope == "user"


@pytest.mark.parametrize(
    "config_name, expected",
    (
        ("config.yaml", "/home/agent/config.yaml"),
        ("config-all.yaml", "/home/agent/config-all.yaml"),
    ),
)
def test_development_worker_fallback_translates_the_sandbox_config_argument(
    tmp_path: Path, config_name: str, expected: str,
) -> None:
    from bianbu_agent.security.runtime_process.supervisor import (
        _sandbox_config_argument,
    )

    data_dir = tmp_path / "data"
    assert _sandbox_config_argument(data_dir / config_name, data_dir) == expected
    # A config outside the data root cannot be named relative to /home/agent;
    # keep the legacy canonical path the unmanaged fallback already handled.
    assert (
        _sandbox_config_argument(tmp_path / "elsewhere" / "runtime.yaml", data_dir)
        == "/home/agent/config.yaml"
    )


@pytest.mark.asyncio
async def test_control_runtime_rejects_unimplemented_sandbox_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "data_dir: .bianbu-agent\n"
        "runtime:\n"
        "  control:\n"
        "    execution:\n"
        "      requested_mode: sandbox\n",
        encoding="utf-8",
    )
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.create_runtime(config, role="control")
    with pytest.raises(bianbu_agent.RuntimeStartupError, match="控制面 sandbox 尚未实现"):
        await runtime.start()


@pytest.mark.asyncio
async def test_control_runtime_starts_without_eager_agent_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bianbu_agent.security.runtime_process.supervisor as supervisor_module

    config_path = _write_config(tmp_path)
    await _prepare_runtime_data(config_path)
    config = bianbu_agent.load_runtime_config(config_path)

    async def fail_start(worker):
        raise AssertionError("No system Bibit is installed; no Worker should start")

    monkeypatch.setattr(supervisor_module.WorkerSupervisor, "start", fail_start)
    runtime = bianbu_agent.create_runtime(config, role="control")
    await runtime.start()
    try:
        assert not runtime.worker_supervisor.ready
        assert not runtime.worker_supervisor._warmups
    finally:
        await runtime.close()


def test_runtime_app_exposes_controlled_execution_lifecycle_api(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "data_dir: .bianbu-agent\n",
        encoding="utf-8",
    )
    config = bianbu_agent.load_runtime_config(config_path)
    api_paths = bianbu_agent.create_runtime_app(config, manage_lifespan=False).openapi()["paths"]
    assert {
        "/api/v1/agents/{agent_id}/executions",
        "/api/v1/executions",
        "/api/v1/executions/{instance_id}",
        "/api/v1/executions/{instance_id}/stop",
        "/api/v1/agents/{agent_id}/revoke",
    } <= set(api_paths)
    assert "/api/v1/runtime/status" in api_paths
    assert "/api/v1/runtime/workers" in api_paths


@pytest.mark.asyncio
async def test_runtime_status_exposes_control_and_worker_readiness_without_secrets(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "data_dir: .bianbu-agent\n",
        encoding="utf-8",
    )
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.create_runtime(config, role="control")
    runtime._started = True
    runtime.worker_supervisor = SimpleNamespace(
        ready=False,
        status_snapshot=lambda: {
            "state": "failed", "instance_id": "com.bianbu.agent-x",
            "pid": 123, "effective_mode": "sandbox", "backend": "bwrap",
            "sandboxed": True,
            "generation_id": None,
        },
    )
    from bianbu_agent.server.app import create_app
    app = create_app(runtime, manage_lifespan=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/runtime/status")
        health = await client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["status"] == "DEGRADED"
    assert payload["control_ready"] is True
    assert payload["agent_ready"] is False
    assert payload["backend"] == "bwrap"
    # 系统级服务整机一份：调用者要能问出这份 Runtime 服务的账号和数据目录，
    # 而不是假设 ~/.bianbu-agent 一定是自己的。
    assert payload["data_dir"] == str(config.data_dir)
    assert payload["service_user"] == ""
    assert "lease_id" not in payload
    assert health.status_code == 200
    assert health.json()["status"] == "DEGRADED"


@pytest.mark.asyncio
async def test_runtime_status_reports_the_system_service_user(
    tmp_path: Path, monkeypatch,
) -> None:
    """``BIANBU_RUNTIME_SERVICE_USER`` is the account the device resolved."""

    config_path = tmp_path / "runtime.yaml"
    config_path.write_text("data_dir: .bianbu-agent\n", encoding="utf-8")
    monkeypatch.setenv("BIANBU_RUNTIME_SERVICE_USER", " bianbu ")
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.create_runtime(config, role="control")
    runtime._started = True
    from bianbu_agent.server.app import create_app
    app = create_app(runtime, manage_lifespan=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = (await client.get("/api/v1/runtime/status")).json()["data"]
    assert payload["service_user"] == "bianbu"
    assert payload["data_dir"] == str(config.data_dir)


@pytest.mark.asyncio
async def test_runtime_workers_exposes_control_and_all_worker_details_without_secrets(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text("data_dir: .bianbu-agent\n", encoding="utf-8")
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.create_runtime(config, role="control")
    runtime._started = True
    runtime.worker_supervisor = SimpleNamespace(
        ready=False,
        status_snapshot=lambda: {
            "state": "running",
            "worker_count": 2,
            "workers": {
                "agent-a": {"state": "running", "instance_id": "i-a", "pid": 101,
                             "effective_mode": "sandbox", "backend": "bwrap",
                             "sandboxed": True, "generation_id": 2, "lease_id": "secret",
                             "memory_registration": {"state": "degraded", "error": "disk unavailable"}},
                "agent-b": {"state": "failed", "instance_id": "i-b", "pid": None,
                             "effective_mode": "host", "backend": "host-process",
                             "sandboxed": False, "generation_id": None, "error": "boom"},
            },
        }
    )
    from bianbu_agent.server.app import create_app
    app = create_app(runtime, manage_lifespan=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/runtime/workers")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["runtime"]["status"] == "CONTROL_READY"
    assert payload["control"]["pid"] > 0
    assert payload["control"]["control_ready"] is True
    assert payload["control"]["sandboxed"] is False
    assert payload["control"]["effective_mode"] == "host"
    assert payload["control"]["backend"] == "control-process"
    assert payload["worker_count"] == 2
    assert [item["agent_id"] for item in payload["workers"]] == ["agent-a", "agent-b"]
    assert payload["workers"][0]["sandboxed"] is True
    assert payload["workers"][1]["error"] == "boom"
    assert payload["workers"][0]["memory_registration"]["state"] == "degraded"
    assert "lease_id" not in response.text


@pytest.mark.asyncio
async def test_control_run_manager_joins_run_before_deleting_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a Session mid-Run must not strand the Run stream.

    The store drops every Run projection with the Session, so a stream that
    lands afterwards can only report an unknown request_id: it used to escape
    as an unretrieved ``KeyError`` and a failed Run in the Console.
    """

    from bianbu_agent.security.runtime_process.supervisor import ControlRunManager

    order: list[str] = []

    class Store:
        deleted = False

        async def start(self, request_id, session_id, request):
            order.append("start")

        async def append(self, request_id, event):
            if self.deleted:
                raise KeyError(request_id)
            order.append("append")

        async def finish(self, request_id, result):
            if self.deleted:
                raise KeyError(request_id)
            order.append("finish")

        async def get(self, request_id):
            return None

        async def list(self, session_id=None):
            return []

        async def delete_session(self, session_id):
            order.append("delete_session")
            self.deleted = True
            return 1

    entered = asyncio.Event()

    class Client:
        cancelled = asyncio.Event()

        async def run_start(self, payload, on_event):
            entered.set()
            await on_event({"type": "run.accepted", "run_id": payload["run_id"]})
            await self.cancelled.wait()
            await on_event({
                "type": "run.result", "run_id": payload["run_id"],
                "result": {
                    "request_id": payload["run_id"], "status": "cancelled",
                },
            })

        async def cancel(self, run_id):
            self.cancelled.set()
            return True

    class Supervisor:
        ready = True
        client = Client()

        async def start(self):
            return None

    monkeypatch.setenv("BIANBU_AGENT_MEMORY_ACCOUNT_ID", "system-user-42")
    store = Store()
    manager = ControlRunManager(store, Supervisor())
    handle = await manager.run("session-1", "hello", request_id="run-1")
    await entered.wait()

    await manager.delete_session("session-1")

    assert order.index("finish") < order.index("delete_session")
    assert manager.active_count == 0
    assert handle._finished.is_set()


@pytest.mark.asyncio
async def test_run_handle_finish_tolerates_a_deleted_session() -> None:
    """A Run whose Session vanished still finishes locally instead of raising."""

    from bianbu_agent.runs import RunHandle, RunResult

    class Store:
        async def finish(self, request_id, result):
            raise KeyError(request_id)

    handle = RunHandle(request_id="run-1", session_id="session-1", store=Store())
    await handle.finish(RunResult(request_id="run-1", status="completed", content="ok"))

    assert handle._finished.is_set()
    assert (await handle.wait()).content == "ok"


@pytest.mark.asyncio
async def test_control_run_manager_forwards_events_and_result_over_worker_rpc(monkeypatch) -> None:
    from bianbu_agent.security.runtime_process.supervisor import ControlRunManager

    class Store:
        def __init__(self):
            self.records = {}

        async def start(self, request_id, session_id, request):
            self.records[request_id] = {
                "id": request_id,
                "session_id": session_id,
                "request": dict(request),
            }

        async def append(self, request_id, event):
            self.records[request_id].setdefault("events", []).append(event)

        async def finish(self, request_id, result):
            self.records[request_id]["result"] = result

        async def get(self, request_id):
            return None

        async def list(self, session_id=None):
            return []

    received: list[dict[str, object]] = []

    class Client:
        async def run_start(self, payload, on_event):
            received.append(dict(payload))
            await on_event({"type": "run.accepted", "run_id": payload["run_id"]})
            await on_event({
                "type": "run.event", "run_id": payload["run_id"],
                "event": {"kind": "token", "payload": {"text": "ok"}},
            })
            await on_event({
                "type": "run.result", "run_id": payload["run_id"],
                "result": {"request_id": payload["run_id"], "status": "succeeded", "content": "ok"},
            })

    class Supervisor:
        ready = True
        client = Client()

        async def start(self):
            return None

    monkeypatch.setenv("BIANBU_AGENT_MEMORY_ACCOUNT_ID", "system-user-42")
    store = Store()
    manager = ControlRunManager(store, Supervisor())
    handle = await manager.run(
        "session-1",
        (
            SessionInboxMessage(
                "message-1",
                "hello",
                skill_ids=("global.skills/review",),
            ),
        ),
        request_id="run-1",
        thread_id="thread-1",
    )
    result = await handle.wait()
    assert result.status == "succeeded"
    assert result.content == "ok"
    assert [event.kind async for event in handle] == ["token"]
    assert received[0]["thread_id"] == "thread-1"
    assert received[0]["account_id"] == "system-user-42"
    assert store.records["run-1"]["request"]["account_id"] == "system-user-42"
    assert store.records["run-1"]["request"]["skill_ids"] == [
        "global.skills/review"
    ]


@pytest.mark.asyncio
async def test_control_run_manager_routes_runs_to_agent_worker_processes() -> None:
    from bianbu_agent.security.runtime_process.supervisor import ControlRunManager

    class Store:
        def __init__(self):
            self.records = {}

        async def start(self, request_id, session_id, request):
            self.records[request_id] = {
                "id": request_id,
                "session_id": session_id,
                "request": dict(request),
            }

        async def append(self, request_id, event):
            self.records[request_id].setdefault("events", []).append(event)

        async def finish(self, request_id, result):
            self.records[request_id]["result"] = result

        async def get(self, request_id):
            return None

        async def list(self, session_id=None):
            return []

    class Client:
        def __init__(self, agent_id: str):
            self.agent_id = agent_id
            self.payloads: list[dict[str, object]] = []

        async def run_start(self, payload, on_event):
            self.payloads.append(dict(payload))
            await on_event({
                "type": "run.result",
                "run_id": payload["run_id"],
                "result": {
                    "request_id": payload["run_id"],
                    "status": "succeeded",
                    "content": self.agent_id,
                },
            })

    class Supervisor:
        def __init__(self, agent_id: str):
            self.agent_id = agent_id
            self.ready = True
            self.client = Client(agent_id)

        async def start(self):
            return None

    class Pool:
        def __init__(self):
            self.supervisors: dict[str, Supervisor] = {}

        def for_agent(self, agent_id: str):
            return self.supervisors.setdefault(agent_id, Supervisor(agent_id))

    pool = Pool()
    manager = ControlRunManager(Store(), pool)

    default_handle = await manager.run(
        "session-1", "hello", request_id="run-default", agent_id="default",
    )
    research_handle = await manager.run(
        "session-2", "hello", request_id="run-research", agent_id="research",
    )

    assert (await default_handle.wait()).content == "default"
    assert (await research_handle.wait()).content == "research"
    assert set(pool.supervisors) == {"default", "research"}
    assert pool.supervisors["default"] is not pool.supervisors["research"]
    assert pool.supervisors["default"].client.payloads[0]["agent_id"] == "default"
    assert pool.supervisors["research"].client.payloads[0]["agent_id"] == "research"


@pytest.mark.asyncio
async def test_agent_worker_pool_reloads_each_agent_worker() -> None:
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool

    class Client:
        def __init__(self):
            self.reload_calls = 0

        async def reload(self, *, force=False):
            self.reload_calls += 1
            return {
                "status": "active",
                "generation_id": self.reload_calls,
                "changed": True,
                "force": force,
            }

    class Supervisor:
        def __init__(self):
            self.ready = True
            self.client = Client()

        async def start(self):
            return None

    pool = AgentWorkerPool(SimpleNamespace(agent_id="default"))
    pool._supervisors = {
        "default": Supervisor(),
        "research": Supervisor(),
    }

    result = await pool.reload_installed(force=True)

    assert result["worker_count"] == 2
    assert pool._supervisors["default"].client.reload_calls == 1
    assert pool._supervisors["research"].client.reload_calls == 1


@pytest.mark.asyncio
async def test_agent_worker_pool_isolates_one_reload_failure() -> None:
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool

    class Client:
        def __init__(self, *, fails: bool = False):
            self.fails = fails
            self.reload_calls = 0

        async def reload(self, *, force=False):
            self.reload_calls += 1
            if self.fails:
                raise RuntimeError("candidate rejected")
            return {"generation_id": 2, "changed": True}

    class Supervisor:
        def __init__(self, agent_id: str, *, fails: bool = False):
            self.agent_id = agent_id
            self.ready = True
            self.client = Client(fails=fails)

    pool = AgentWorkerPool(SimpleNamespace(agent_id="default"))
    pool._supervisors = {
        "default": Supervisor("default", fails=True),
        "research": Supervisor("research"),
    }

    result = await pool.reload_installed()

    assert result["worker_count"] == 2
    assert result["reloaded_worker_count"] == 1
    assert result["failures"] == [
        {"agent_id": "default", "error": "candidate rejected"}
    ]
    assert pool._supervisors["default"].catalog_stale is True
    assert pool._supervisors["research"].catalog_stale is False
    assert pool._supervisors["research"].client.reload_calls == 1


@pytest.mark.asyncio
async def test_agent_worker_pool_applies_learning_policy_to_matching_security_boundary(
    tmp_path: Path,
) -> None:
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool

    class Coordinator:
        def __init__(self, active):
            self.active = active
            self.calls = []

        async def apply_network_learning_policy(self, agent_id, mode, version):
            self.calls.append((agent_id, mode, version))
            return {"active_instances": self.active,
                    "collectors_started": self.active,
                    "collectors_stopped": 0, "degraded_instances": 0}

    class Supervisor:
        def __init__(self, agent_id, security_agent_id, active):
            self.agent_id = agent_id
            self.security_agent_id = security_agent_id
            self.coordinator = Coordinator(active)

    pool = AgentWorkerPool(RuntimeConfig(data_dir=tmp_path))
    first = Supervisor("default", "com.bianbu.runtime", 1)
    second = Supervisor("research", "com.bianbu.runtime", 1)
    unrelated = Supervisor("other", "com.other.runtime", 1)
    pool._supervisors = {
        "default": first, "research": second, "other": unrelated,
    }

    result = await pool.apply_network_learning_policy(
        "research", "learning", 12,
    )

    assert result == {"active_instances": 1, "collectors_started": 1,
                      "collectors_stopped": 0, "degraded_instances": 0}
    assert first.coordinator.calls == []
    assert second.coordinator.calls == [("com.bianbu.runtime", "learning", 12)]
    assert unrelated.coordinator.calls == []


@pytest.mark.asyncio
async def test_agent_worker_pool_closes_and_forgets_deleted_agent_worker(
    tmp_path: Path,
) -> None:
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool

    class Supervisor:
        def __init__(self):
            self.closed = False

        async def close(self, *, strict=False):
            self.closed = True

    pool = AgentWorkerPool(RuntimeConfig(data_dir=tmp_path))
    default = Supervisor()
    research = Supervisor()
    pool._supervisors = {"default": default, "research": research}

    removed = await pool.remove_agent("research")

    assert removed is True
    assert research.closed is True
    assert pool._supervisors == {"default": default}
    assert await pool.remove_agent("research") is False
    with pytest.raises(RuntimeError, match="deleted"):
        pool.for_agent("research")
    pool.activate_agent("research")
    assert pool.for_agent("research") is not research
    assert await pool.remove_agent("default") is True




@pytest.mark.asyncio
async def test_agent_worker_pool_keeps_failed_worker_retired_for_retry(
    tmp_path: Path,
) -> None:
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool

    class Supervisor:
        async def close(self, *, strict=False):
            assert strict is True
            raise RuntimeError("worker did not stop")

    pool = AgentWorkerPool(RuntimeConfig(data_dir=tmp_path))
    supervisor = Supervisor()
    pool._supervisors["research"] = supervisor

    with pytest.raises(RuntimeError, match="did not stop"):
        await pool.remove_agent("research")

    assert pool._supervisors["research"] is supervisor
    with pytest.raises(RuntimeError, match="deleted"):
        pool.for_agent("research")


@pytest.mark.asyncio
async def test_control_run_manager_fails_only_runs_for_failed_worker() -> None:
    from bianbu_agent.security.runtime_process.supervisor import ControlRunManager

    class Store:
        def __init__(self):
            self.results = {}

        async def start(self, request_id, session_id, request):
            return None

        async def append(self, request_id, event):
            return None

        async def finish(self, request_id, result):
            self.results[request_id] = result

        async def get(self, request_id):
            return None

        async def list(self, session_id=None):
            return []

    class Client:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def run_start(self, payload, on_event):
            self.entered.set()
            await self.release.wait()
            await on_event({
                "type": "run.result",
                "run_id": payload["run_id"],
                "result": {
                    "request_id": payload["run_id"],
                    "status": "succeeded",
                    "content": "ok",
                },
            })

    class Supervisor:
        def __init__(self):
            self.ready = True
            self.client = Client()

        async def start(self):
            return None

    class Pool:
        def __init__(self):
            self.supervisors = {
                "default": Supervisor(),
                "research": Supervisor(),
            }

        def for_agent(self, agent_id: str):
            return self.supervisors[agent_id]

    pool = Pool()
    store = Store()
    manager = ControlRunManager(store, pool)
    default = await manager.run(
        "session-1", "hello", request_id="run-default", agent_id="default",
    )
    research = await manager.run(
        "session-2", "hello", request_id="run-research", agent_id="research",
    )
    await pool.supervisors["default"].client.entered.wait()
    await pool.supervisors["research"].client.entered.wait()

    await manager.fail_active("crashed", supervisor=pool.supervisors["default"])

    assert (await default.wait()).status == "failed"
    assert not research._finished.is_set()
    pool.supervisors["research"].client.release.set()
    assert (await research.wait()).status == "succeeded"
    await manager.close()


@pytest.mark.asyncio
async def test_control_run_manager_surfaces_worker_rpc_failure_as_terminal_error() -> None:
    """A dead Worker must fail visibly instead of leaving the UI generating."""

    from bianbu_agent.security.runtime_process.supervisor import ControlRunManager

    class Store:
        def __init__(self):
            self.result = None

        async def start(self, request_id, session_id, request):
            return None

        async def append(self, request_id, event):
            return None

        async def finish(self, request_id, result):
            self.result = result

        async def get(self, request_id):
            return None

        async def list(self, session_id=None):
            return []

    class Client:
        async def run_start(self, payload, on_event):
            raise ConnectionError("worker stream disconnected")

    class Supervisor:
        ready = True
        client = Client()

        async def start(self):
            return None

    store = Store()
    manager = ControlRunManager(store, Supervisor())
    handle = await manager.run("session-1", "hello", request_id="run-failed")

    result = await handle.wait()

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "worker_rpc_error"
    assert result.error.message == "worker stream disconnected"
    assert store.result is result


@pytest.mark.asyncio
async def test_control_run_manager_names_cgroup_memory_limit_as_cause() -> None:
    """Worker 触顶 memcg 限额被杀时不能只剩一句裸的 EOFError。"""

    from types import SimpleNamespace

    from bianbu_agent.security.runtime_process.supervisor import ControlRunManager

    class Store:
        def __init__(self):
            self.result = None

        async def start(self, request_id, session_id, request):
            return None

        async def append(self, request_id, event):
            return None

        async def finish(self, request_id, result):
            self.result = result

        async def get(self, request_id):
            return None

        async def list(self, session_id=None):
            return []

    class Client:
        async def run_start(self, payload, on_event):
            raise EOFError

    class Supervisor:
        ready = True
        client = Client()
        record = SimpleNamespace(unit="bianbu-agent-com.bianbu.runtime-abc.scope")

        async def start(self):
            return None

        def memory_limit_reason(self):
            return "Worker 因内存限额被杀（MemoryMax=2.00 GiB，MemoryPeak=1.93 GiB）"

    store = Store()
    manager = ControlRunManager(store, Supervisor())
    handle = await manager.run("session-1", "hello", request_id="run-oom")

    result = await handle.wait()

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "worker_oom_killed"
    assert "内存限额" in result.error.message
    assert result.error.details["exception"] == "EOFError"
    assert str(result.error.details["unit"]).endswith(".scope")
    assert store.result is result


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('maintenance', [False, True])
async def test_upgrade_disconnect_attribution(monkeypatch, wrapped, maintenance):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from bianbu_agent.security.runtime_process.supervisor import ControlRunManager
    from bianbu_agent.security.runtime_process.worker_rpc import WorkerRPCError
    from bianbu_agent.server import maintenance as upgrade

    monkeypatch.setattr(upgrade, 'upgrade_snapshot', AsyncMock(return_value={'maintenance': maintenance}))
    error = WorkerRPCError('worker_rpc_error', 'EOFError') if wrapped else EOFError()
    store = SimpleNamespace(start=AsyncMock(), append=AsyncMock(), finish=AsyncMock(),
                            get=AsyncMock(return_value=None), list=AsyncMock(return_value=[]))
    supervisor = SimpleNamespace(ready=True, start=AsyncMock(),
                                 memory_limit_reason=lambda: None,
                                 client=SimpleNamespace(run_start=AsyncMock(side_effect=error)))
    manager = ControlRunManager(store, supervisor)
    result = await (await manager.run('session', 'hello', request_id='upgrade')).wait()
    assert result.status == 'failed'
    assert result.error.code == ('runtime_upgrading' if maintenance else 'worker_rpc_error')


@pytest.mark.asyncio
async def test_agent_worker_executes_control_owned_session_without_persistence() -> None:
    from bianbu_agent.security.runtime_process.worker import AgentWorker

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Handle:
        async def wait(self):
            return None

    class Runs:
        async def run(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Handle()

        async def get(self, _run_id):
            return None

    class Sessions:
        async def get_or_create(self, **_kwargs):
            raise AssertionError("Worker must not read or create Session metadata")

    worker = AgentWorker.__new__(AgentWorker)
    worker.runtime = SimpleNamespace(runs=Runs(), sessions=Sessions(),
                                     plugin_runtime=SimpleNamespace(reload_installed=lambda **_: asyncio.sleep(0)))
    worker._activated = True
    worker._active = {}

    handle = await worker.dispatch("run.start", {
        "run_id": "run-1",
        "session_id": "session-1",
        "thread_id": "thread-1",
        "agent_id": "default",
        "account_id": "system-user-42",
        "prompt": "hello",
        "metadata": {"source": "test"},
        "idempotency_key": "idem-1",
        "idempotency_fingerprint": "fingerprint-1",
    })

    assert isinstance(handle, Handle)
    assert calls == [(('session-1', 'hello'), {
        "agent_id": "default",
        "account_id": "system-user-42",
        "thread_id": "thread-1",
        "metadata": {"source": "test"},
        "idempotency_key": "idem-1",
        "idempotency_fingerprint": "fingerprint-1",
        "request_id": "run-1",
    })]
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_worker_reload_prepares_servers_without_an_explicit_refresh() -> None:
    """Publishing a mutation is a reload; the new server must be started then.

    ``mcp add`` publishes through a plain reload, so restricting the
    re-preparation to an explicit refresh left a freshly installed server at
    ``not_loaded`` with nothing running it until the Worker restarted.
    """

    from bianbu_agent.security.runtime_process.worker import AgentWorker

    prepared: list[str] = []
    reloads: list[bool] = []

    async def reload_installed(*, force: bool = False):
        reloads.append(force)
        return SimpleNamespace(generation_id=7, changed=True)

    worker = AgentWorker.__new__(AgentWorker)
    worker._activated = True
    worker.runtime = SimpleNamespace(
        plugin_runtime=SimpleNamespace(reload_installed=reload_installed),
        prepare_installed_mcps=lambda: prepared.append("installed"),
    )

    result = await worker.dispatch("worker.reload", {})

    assert reloads == [False]
    assert prepared == ["installed"]
    assert result["changed"] is True


@pytest.mark.asyncio
async def test_agent_worker_rejects_cross_agent_run_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.security.runtime_process.worker import AgentWorker

    class Runs:
        async def run(self, *args, **kwargs):
            raise AssertionError("cross-agent payload must not execute")

    monkeypatch.setenv("BIANBU_WORKER_AGENT_ID", "research")

    worker = AgentWorker.__new__(AgentWorker)
    worker.runtime = SimpleNamespace(runs=Runs())
    worker._activated = True
    worker._active = {}

    with pytest.raises(RuntimeError, match="bound to Agent research"):
        await worker.dispatch("run.start", {
            "run_id": "run-1",
            "session_id": "session-1",
            "thread_id": "thread-1",
            "agent_id": "default",
            "account_id": "system-user-42",
            "prompt": "hello",
        })


@pytest.fixture
def worker_environment():
    # Worker.start intentionally owns process-wide identity in production.
    # In-process tests must restore all of it, including newly added fields.
    with patch.dict(os.environ):
        yield


@pytest.mark.asyncio
async def test_agent_worker_injects_non_persistent_session_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_environment,
) -> None:
    import bianbu_agent.security.runtime_process.worker as worker_module

    captured: dict[str, object] = {}

    def fake_create_runtime(_config, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(close=lambda: asyncio.sleep(0))

    monkeypatch.setattr(worker_module, "create_runtime", fake_create_runtime)
    monkeypatch.setenv("BIANBU_WORKER_AGENT_ID", "default")
    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    worker = worker_module.AgentWorker(
        SimpleNamespace(), instance_id="instance-1",
        socket_path=tmp_path / "worker.sock",
    )
    await worker.start()
    assert worker._runtime_task is not None
    worker.runtime = await worker._runtime_task
    worker._runtime_task = None
    await worker.close()

    assert isinstance(
        captured["session_store"], worker_module.ControlOwnedSessionStore,
    )


@pytest.mark.parametrize("sandboxed", [False, True])
async def test_agent_worker_only_sandbox_loads_seccomp_audit_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_environment,
    sandboxed: bool,
) -> None:
    """host Worker 保留 sudo/setuid 语义，只有沙箱 Worker 装载审计过滤器。"""
    import bianbu_agent.security.runtime_process.worker as worker_module
    from bianbu_agent.security import seccomp_audit

    def fake_create_runtime(_config, **_kwargs):
        return SimpleNamespace(close=lambda: asyncio.sleep(0))

    loaded: list[str] = []
    monkeypatch.setattr(worker_module, "create_runtime", fake_create_runtime)
    monkeypatch.setattr(
        seccomp_audit, "enable_filter_logging",
        lambda path, **kwargs: loaded.append(str(path)),
    )
    filter_path = tmp_path / "policy" / "seccomp.bpf"
    filter_path.parent.mkdir()
    filter_path.write_bytes(b"\x06\x00\x00\x00\x00\x00\x00\x00")
    monkeypatch.setenv("BIANBU_WORKER_AGENT_ID", "default")
    monkeypatch.setenv("BIANBU_SECCOMP_AUDIT_FILTER", str(filter_path))
    if sandboxed:
        monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "1")
    else:
        monkeypatch.delenv("BIANBU_SANDBOX_ACTIVE", raising=False)

    worker = worker_module.AgentWorker(
        SimpleNamespace(), instance_id="instance-1",
        socket_path=tmp_path / "worker.sock",
    )
    await worker.start()
    worker.runtime = await worker._runtime_task
    worker._runtime_task = None
    await worker.close()

    assert loaded == ([str(filter_path)] if sandboxed else [])


@pytest.mark.asyncio
async def test_agent_worker_exports_agent_security_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_environment,
) -> None:
    import bianbu_agent.security.runtime_process.worker as worker_module

    def fake_create_runtime(_config, **_kwargs):
        return SimpleNamespace(close=lambda: asyncio.sleep(0))

    monkeypatch.setattr(worker_module, "create_runtime", fake_create_runtime)
    monkeypatch.setenv("BIANBU_WORKER_AGENT_ID", "research")
    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.delenv("BIANBU_SECURITY_SUBJECT", raising=False)

    worker = worker_module.AgentWorker(
        SimpleNamespace(), instance_id="instance-1",
        socket_path=tmp_path / "worker.sock",
    )
    await worker.start()
    try:
        assert os.environ["COM_BIANBU_AGENT_ID"] == "research"
        assert os.environ["BIANBU_SECURITY_SUBJECT"] == (
            "com.bianbu.agent_id:research"
        )
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_agent_worker_creates_runtime_for_bound_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_environment,
) -> None:
    import bianbu_agent.security.runtime_process.worker as worker_module

    captured: dict[str, object] = {}

    def fake_create_runtime(_config, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(close=lambda: asyncio.sleep(0))

    monkeypatch.setattr(worker_module, "create_runtime", fake_create_runtime)
    monkeypatch.setenv("BIANBU_WORKER_AGENT_ID", "research")
    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.delenv("BIANBU_SECURITY_SUBJECT", raising=False)

    worker = worker_module.AgentWorker(
        SimpleNamespace(), instance_id="instance-1",
        socket_path=tmp_path / "worker.sock",
    )
    await worker.start()
    worker.runtime = await worker._runtime_task
    worker._runtime_task = None
    await worker.close()

    assert captured["agent_id"] == "research"


def test_agent_worker_catalog_marks_unavailable_skill_disabled() -> None:
    """The production Worker catalog must match Run availability checks."""

    from bianbu_agent.security.runtime_process.worker import AgentWorker

    skill_id = "global.skills/review"
    generation = PluginGeneration(
        id=1,
        plugins={
            "global.skills": PluginRuntime(
                plugin_id="global.skills",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    skills=(
                        SkillContribution(
                            "global.skills",
                            SkillSpec(
                                name="review",
                                metadata={
                                    "qualified_id": skill_id,
                                    "unavailable_reason": "dependency missing",
                                },
                            ),
                        ),
                    ),
                ),
            ),
        },
        statuses={},
    )
    worker = AgentWorker.__new__(AgentWorker)
    worker.runtime = SimpleNamespace(
        plugin_runtime=SimpleNamespace(current_generation=generation)
    )

    [skill] = worker._catalog_views()["skill"]
    assert skill["id"] == skill_id
    assert skill["enabled"] is False
    assert skill["unavailable_reason"] == "dependency missing"


async def _noop_tool_handler(_context, _arguments):
    raise NotImplementedError


def _catalog_worker_with_v2_fields():
    """A Worker whose catalog mixes a builtin Tool and a v2 Skill.

    ``ToolSpec`` has no ``name_zh`` / ``name_en`` / ``description_zh`` fields, so
    the Tool can only carry them through ``metadata``; ``SkillSpec`` has them as
    real fields.  Reading ``spec.name_zh`` unchanged raised ``AttributeError``
    for every builtin Tool, which failed the whole ``worker.catalog`` RPC: the
    Console and the BiBit plugin then reported every MCP server as ``unknown``.
    """

    from bianbu_agent.inventory.catalog import CapabilityCatalog
    from bianbu_agent.inventory.models import (
        CapabilityKind,
        CapabilityOrigin,
        CapabilitySource,
    )
    from bianbu_agent.plugins import ToolContribution, ToolSpec
    from bianbu_agent.security.runtime_process.worker import AgentWorker

    plugin_id = "global.capabilities"
    tool_id = "global.capabilities/echo"
    skill_id = "global.capabilities/review"
    root = Path("/tmp/nonexistent-capability-root")
    tool_spec = ToolSpec(
        "echo", "Echo", {"type": "object"}, {"qualified_id": tool_id}
    )
    generation = PluginGeneration(
        id=1,
        plugins={
            plugin_id: PluginRuntime(
                plugin_id=plugin_id,
                instance=object(),
                config={},
                contributions=PluginContributions(
                    skills=(
                        SkillContribution(
                            plugin_id,
                            SkillSpec(
                                name="review",
                                name_zh="代码审查",
                                name_en="Code review",
                                description_zh="按清单审查改动",
                                metadata={"qualified_id": skill_id},
                            ),
                        ),
                    ),
                ),
            ),
        },
        statuses={},
        capability_catalog=CapabilityCatalog(
            tools={
                tool_id: ToolContribution(
                    plugin_id=plugin_id, spec=tool_spec, handler=_noop_tool_handler
                )
            },
            skills={},
            sources={
                (CapabilityKind.TOOL, tool_id): CapabilitySource(
                    kind=CapabilityKind.TOOL,
                    capability_id=tool_id,
                    version="1.0.0",
                    root=root,
                    manifest_path=root / "tool.json",
                    content_digest="0" * 64,
                    origin=CapabilityOrigin.PLUGIN,
                    plugin_id=plugin_id,
                )
            },
            plugin_sources={
                plugin_id: CapabilitySource(
                    kind=CapabilityKind.TOOL,
                    capability_id=plugin_id,
                    version="1.0.0",
                    root=root,
                    manifest_path=root / "plugin.yaml",
                    content_digest="0" * 64,
                    origin=CapabilityOrigin.PLUGIN,
                    plugin_id=plugin_id,
                )
            },
            inventory_revision=1,
        ),
    )
    worker = AgentWorker.__new__(AgentWorker)
    worker.runtime = SimpleNamespace(
        plugin_runtime=SimpleNamespace(current_generation=generation)
    )
    return worker, tool_id, skill_id


def test_agent_worker_catalog_tolerates_specs_without_v2_display_fields() -> None:
    worker, tool_id, _skill_id = _catalog_worker_with_v2_fields()

    views = worker._catalog_views()
    [tool] = views["tool"]
    assert tool["id"] == tool_id
    assert tool["name_zh"] == ""
    assert tool["description_zh"] == ""


def test_agent_worker_catalog_returns_skill_v2_display_fields() -> None:
    worker, _tool_id, skill_id = _catalog_worker_with_v2_fields()

    [skill] = worker._catalog_views()["skill"]
    assert skill["id"] == skill_id
    assert skill["name_zh"] == "代码审查"
    assert skill["name_en"] == "Code review"
    assert skill["description_zh"] == "按清单审查改动"


def test_agent_worker_catalog_reads_v2_display_fields_from_tool_metadata() -> None:
    """A Tool without v2 fields passes them through ``metadata`` instead."""

    worker, tool_id, _skill_id = _catalog_worker_with_v2_fields()
    contribution = worker.runtime.plugin_runtime.current_generation.capability_catalog.tools[tool_id]
    _spec = contribution.spec

    assert worker._display_field(_spec, "name_zh") == ""
    assert worker._display_field(
        SimpleNamespace(
            metadata={"name_zh": "搜索", "description_zh": "搜索资料"}
        ),
        "name_zh",
    ) == "搜索"
    assert worker._display_field(
        SimpleNamespace(metadata={"description_zh": "搜索资料"}),
        "description_zh",
    ) == "搜索资料"
    assert worker._display_field(SimpleNamespace(), "name_en") == ""


@pytest.mark.asyncio
async def test_control_run_manager_does_not_reconcile_active_worker_run_as_orphan() -> None:
    """The initial POST /runs snapshot must not race the Worker stream."""

    from bianbu_agent.runs.models import StoredRun
    from bianbu_agent.security.runtime_process.supervisor import ControlRunManager

    class Store:
        def __init__(self):
            self.record = None

        async def start(self, request_id, session_id, request):
            import time

            self.record = StoredRun(
                request_id=request_id,
                session_id=session_id,
                status="running",
                request=dict(request),
                events=(),
                result=None,
                created_at=time.time(),
                updated_at=time.time(),
            )

        async def append(self, request_id, event):
            return None

        async def finish(self, request_id, result):
            return None

        async def get(self, request_id):
            return self.record

        async def list(self, session_id=None):
            return []

    finished = asyncio.Event()

    class Client:
        async def run_start(self, payload, on_event):
            await finished.wait()
            await on_event({
                "type": "run.result",
                "run_id": payload["run_id"],
                "result": {
                    "request_id": payload["run_id"],
                    "status": "completed",
                    "content": "ok",
                },
            })

    class Supervisor:
        ready = True
        client = Client()

        async def start(self):
            return None

    store = Store()
    manager = ControlRunManager(store, Supervisor())
    handle = await manager.run("session-active", "hello", request_id="run-active")
    active_record = await manager.get_record("run-active")
    assert active_record is store.record
    assert active_record.status == "running"
    assert active_record.result is None
    finished.set()
    await handle.wait()
    await manager.close()


@pytest.mark.asyncio
async def test_execution_start_rejects_untrusted_command_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "data_dir: .bianbu-agent\n",
        encoding="utf-8",
    )
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.create_runtime(config)
    from bianbu_agent.server.app import create_app
    import httpx

    transport = httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agents/example/executions",
            json={"requested_mode": "host", "command": ["/bin/sh", "-c", "id"]},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "extra_forbidden" in response.text


@pytest.mark.asyncio
async def test_execution_start_rejects_supervisor_reserved_environment(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "data_dir: .bianbu-agent\n",
        encoding="utf-8",
    )
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.create_runtime(config)
    from bianbu_agent.server.app import create_app
    import httpx

    transport = httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/agents/example/executions",
            json={"env": {"BIANBU_WORKER_RPC_TOKEN": "forged"}},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "Runtime-reserved" in response.text


@pytest.mark.asyncio
async def test_security_identity_is_bound_by_runtime_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", str(tmp_path / "securityd"))
    monkeypatch.setenv("BIANBU_RUNTIME_SECURITY_ID", "com.bianbu.runtime-test")
    monkeypatch.setitem(
        sys.modules,
        "bianbu_security_client",
        SimpleNamespace(bind_agent=lambda agent_id: calls.append(agent_id) or True),
    )

    assert await _bind_security_identity() is True
    assert calls == ["com.bianbu.runtime-test"]


@pytest.mark.asyncio
async def test_security_identity_binding_is_optional_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANBU_SECURITY", "0")
    monkeypatch.delenv("BIANBU_SECURITYD_HOME", raising=False)
    monkeypatch.delenv("BIANBU_AGENT_ID", raising=False)

    assert await _bind_security_identity() is False


def test_product_runtime_composes_without_global_default_model(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = bianbu_agent.load_runtime_config(config_path)

    runtime = bianbu_agent.create_runtime(config)

    assert runtime.default_model is None
    # A Worker owns the execution graph and, with it, the background start of
    # the MCP servers every declaration asks for.
    assert [
        type(service).__module__ for service in runtime._managed_plugin_services
    ] == ["bianbu_agent.tools.runtime", "bianbu_agent.mcp.preparation"]


@pytest.mark.asyncio
async def test_worker_prepares_every_installed_mcp_server(tmp_path: Path) -> None:
    """``mcp add`` only registers; the Worker installs and starts it later.

    AgentHub kills an Agent CLI call after 30 seconds, so an install cannot be
    the one that vendors an npm tree.  The Worker is: it walks the installed
    catalog on every start, whether or not a selected Agent declares the
    server, and leaves the npm work to its own background task.
    """

    config_path = _write_config(tmp_path)
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.create_runtime(config)
    staging = tmp_path / "package"
    staging.mkdir()
    package = write_managed_mcp_package(
        ManagedMCPDefinition(
            plugin_id="mcp.weather",
            display_name="Weather",
            executable="/bin/echo",
        ),
        staging,
    )
    runtime.mcps.install(package)

    runtime.prepare_installed_mcps()

    preparation = runtime.mcp_preparation
    assert preparation is not None
    try:
        state = preparation.state("mcp.weather")
        assert state is not None and state.ready
    finally:
        await preparation.close()


@pytest.mark.asyncio
async def test_product_run_does_not_fallback_to_legacy_default_model(
    tmp_path: Path,
) -> None:
    """A model-less repair Agent cannot execute through an old default Model record."""

    config_path = _write_config(tmp_path)
    await _prepare_runtime_data(config_path)
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.BianbuAgentRuntime(
        config=config,
        default_model=None,
        agent_id="default",
        model_resolver=YAMLModelResolver(config.data_dir / "models"),
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="default", name="Default", model=None),)
        ),
        engine_builder=lambda *_: object(),
    )
    await runtime.start()
    try:
        with pytest.raises(AgentValidationError, match="agent_model_required"):
            await runtime.runs.run(
                "session", "must not execute", agent_id="default"
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_worker_runtime_uses_explicit_agent_id_for_memory_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path)
    await _prepare_runtime_data(config_path)
    config = bianbu_agent.load_runtime_config(config_path)
    monkeypatch.setenv("BIANBU_AGENT_MEMORY_REGISTRATION", "1")
    monkeypatch.delenv("BIANBU_AGENT_MEMORY_AGENT_NAME", raising=False)
    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.delenv("BIANBU_WORKER_AGENT_ID", raising=False)
    registered: list[str | None] = []

    def fake_register(*, agent_name: str | None = None):
        registered.append(agent_name)
        return None

    monkeypatch.setattr(
        "bianbu_agent.server.runtime.register_memory_runtime",
        fake_register,
    )
    runtime = bianbu_agent.BianbuAgentRuntime(
        config=config,
        default_model=None,
        agent_id="default",
        model_resolver=YAMLModelResolver(config.data_dir / "models"),
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="default", name="Default", model=None),)
        ),
        engine_builder=lambda *_: object(),
    )
    await runtime.start()
    try:
        assert registered == ["default"]
        assert os.environ["BIANBU_AGENT_MEMORY_AGENT_NAME"] == "default"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_worker_runtime_uses_bound_agent_id_for_memory_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path)
    await _prepare_runtime_data(config_path)
    config = bianbu_agent.load_runtime_config(config_path)
    monkeypatch.setenv("BIANBU_AGENT_MEMORY_REGISTRATION", "1")
    monkeypatch.setenv("COM_BIANBU_AGENT_ID", "research")
    monkeypatch.delenv("BIANBU_AGENT_MEMORY_AGENT_NAME", raising=False)
    registered: list[str | None] = []

    def fake_register(*, agent_name: str | None = None):
        registered.append(agent_name)
        return None

    monkeypatch.setattr(
        "bianbu_agent.server.runtime.register_memory_runtime",
        fake_register,
    )
    runtime = bianbu_agent.BianbuAgentRuntime(
        config=config,
        default_model=None,
        agent_id="research",
        model_resolver=YAMLModelResolver(config.data_dir / "models"),
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="research", name="Research", model=None),)
        ),
        engine_builder=lambda *_: object(),
    )
    await runtime.start()
    try:
        assert registered == ["research"]
        assert os.environ["BIANBU_AGENT_MEMORY_AGENT_NAME"] == "research"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_product_startup_rejects_legacy_model_record_id(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    await _prepare_runtime_data(config_path)
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.BianbuAgentRuntime(
        config=config,
        default_model=None,
        agent_id="default",
        model_resolver=YAMLModelResolver(config.data_dir / "models"),
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="default", name="Default", model="model-default"),)
        ),
        engine_builder=lambda *_: object(),
    )

    with pytest.raises(
        bianbu_agent.RuntimeStartupError,
        match="Agent model is unavailable: model-default",
    ):
        await runtime.start()


@pytest.mark.asyncio
async def test_service_composes_runtime_and_configured_fastapi_app(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    await _prepare_runtime_data(config_path)
    config = bianbu_agent.load_runtime_config(config_path)

    runtime = bianbu_agent.create_runtime(config)

    assert isinstance(runtime, bianbu_agent.BianbuAgentRuntime)
    assert runtime.config is config
    assert not hasattr(runtime, "default_agent_id")

    app = bianbu_agent.create_runtime_app(config, manage_lifespan=False)
    assert app.title == "Configured Runtime"
    assert app.state.runtime.config.data_dir == config.data_dir
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/chat")
    assert response.status_code == 200


def test_service_factory_forwards_explicit_worker_agent_identity(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = bianbu_agent.load_runtime_config(config_path)

    runtime = bianbu_agent.create_runtime(
        config,
        role="worker",
        agent_id="com.bianbu.123456789abc",
    )

    assert runtime._worker_agent_id == "com.bianbu.123456789abc"

@pytest.mark.asyncio
async def test_control_start_only_warms_system_bibit(tmp_path, monkeypatch):
    from bianbu_agent import provisioning
    from bianbu_agent.server.app import create_app
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor
    config_path = _write_config(tmp_path)
    await _prepare_runtime_data(config_path)
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.create_runtime(config, role='control')
    runtime.agent_packages.create(AgentSpec(id='other', name='Other'))
    started = []
    entered, release = asyncio.Event(), asyncio.Event()
    async def start(worker):
        started.append(worker.agent_id)
        entered.set()
        await release.wait()
    monkeypatch.setattr(WorkerSupervisor, 'start', start)
    monkeypatch.setattr(provisioning, 'system_starter_id', lambda _: 'default')
    startup = asyncio.create_task(runtime.start())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(asyncio.shield(startup), 1)
        assert started == ['default']
        # 执行模式由 securityd 按安全设置推导，Runtime 不再自行指定。
        assert runtime.worker_supervisor.for_agent('other').requested_mode == 'auto'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False)), base_url='http://test') as client:
            assert (await client.get('/api/v1/runtime/status')).status_code == 200
    finally:
        release.set()
        await startup
        await runtime.close()


@pytest.mark.asyncio
async def test_catalog_reload_does_not_start_idle_expert(tmp_path, monkeypatch):
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool, WorkerSupervisor
    pool = AgentWorkerPool(bianbu_agent.RuntimeConfig(data_dir=tmp_path))
    pool.activate_agent('idle', requested_mode='sandbox')
    async def fail_start(worker):
        raise AssertionError('Reload must not launch an unused expert')
    monkeypatch.setattr(WorkerSupervisor, 'start', fail_start)
    assert (await pool.reload_installed(force=True))['worker_count'] == 0
    assert not pool.for_agent('idle').ready
    await pool.close()


@pytest.mark.asyncio
async def test_pool_close_waits_for_inflight_warmup_before_cleanup(tmp_path, monkeypatch):
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool, WorkerSupervisor
    pool = AgentWorkerPool(bianbu_agent.RuntimeConfig(data_dir=tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []
    async def start(worker):
        entered.set()
        await release.wait()
        completed.append('spawned')
    async def close(worker, **kwargs):
        completed.append('closed')
    monkeypatch.setattr(WorkerSupervisor, 'start', start)
    monkeypatch.setattr(WorkerSupervisor, 'close', close)
    pool.warm_agent('bibit')
    await entered.wait()
    closing = asyncio.create_task(pool.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert completed == ['spawned', 'closed']
    assert not pool._warmups


def _restart_policy_pool(tmp_path: Path, *, max_restarts: int, backoff_seconds: tuple[float, ...]):
    from bianbu_agent.config import (
        RuntimeConfig,
        RuntimeControlConfig,
        RuntimeProcessConfig,
        RuntimeRestartConfig,
    )
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool

    return AgentWorkerPool(
        RuntimeConfig(
            data_dir=tmp_path,
            runtime=RuntimeProcessConfig(
                control=RuntimeControlConfig(
                    restart_policy=RuntimeRestartConfig(
                        max_restarts=max_restarts,
                        backoff_seconds=backoff_seconds,
                    ),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_warm_agent_retries_transient_start_failure(tmp_path, monkeypatch):
    """A one-shot startup failure (e.g. an expired launch lease) is not terminal."""
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    pool = _restart_policy_pool(tmp_path, max_restarts=3, backoff_seconds=(0.01,))
    attempts: list[str] = []
    recovered = asyncio.Event()

    async def start(worker):
        attempts.append(worker.agent_id)
        if len(attempts) == 1:
            raise RuntimeError("受控 Agent 启动失败：lease 已过期")
        recovered.set()

    monkeypatch.setattr(WorkerSupervisor, "start", start)
    pool.warm_agent("bibit")
    warmup = pool._warmups["bibit"]
    await asyncio.wait_for(recovered.wait(), timeout=5)
    await asyncio.wait_for(warmup, timeout=5)
    assert attempts == ["bibit", "bibit"]
    assert not pool._warmups
    await pool.close()


@pytest.mark.asyncio
async def test_warm_agent_escalates_exhausted_retries(tmp_path, monkeypatch, caplog):
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    pool = _restart_policy_pool(tmp_path, max_restarts=2, backoff_seconds=(0.01,))
    attempts: list[str] = []

    async def start(worker):
        attempts.append(worker.agent_id)
        raise RuntimeError("Worker hello timeout")

    monkeypatch.setattr(WorkerSupervisor, "start", start)
    with caplog.at_level(logging.ERROR, logger="bianbu_agent.security.runtime_process.supervisor"):
        pool.warm_agent("bibit")
        warmup = pool._warmups["bibit"]
        await asyncio.wait_for(warmup, timeout=5)
    assert len(attempts) == 3
    assert "Agent Worker 预热失败" in caplog.text
    assert not pool._warmups
    await pool.close()


@pytest.mark.asyncio
async def test_warm_agent_does_not_retry_when_restart_policy_disabled(tmp_path, monkeypatch):
    from bianbu_agent.config import (
        RuntimeConfig,
        RuntimeControlConfig,
        RuntimeProcessConfig,
        RuntimeRestartConfig,
    )
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool, WorkerSupervisor

    pool = AgentWorkerPool(
        RuntimeConfig(
            data_dir=tmp_path,
            runtime=RuntimeProcessConfig(
                control=RuntimeControlConfig(
                    restart_policy=RuntimeRestartConfig(enabled=False, max_restarts=5),
                ),
            ),
        ),
    )
    attempts: list[str] = []

    async def start(worker):
        attempts.append(worker.agent_id)
        raise RuntimeError("boom")

    monkeypatch.setattr(WorkerSupervisor, "start", start)
    pool.warm_agent("bibit")
    warmup = pool._warmups["bibit"]
    await asyncio.wait_for(warmup, timeout=5)
    assert attempts == ["bibit"]
    await pool.close()


@pytest.mark.asyncio
async def test_pool_close_stops_warmup_backoff_instead_of_waiting_it_out(tmp_path, monkeypatch):
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    pool = _restart_policy_pool(tmp_path, max_restarts=5, backoff_seconds=(30.0,))
    attempted = asyncio.Event()

    async def start(worker):
        attempted.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(WorkerSupervisor, "start", start)
    pool.warm_agent("bibit")
    await asyncio.wait_for(attempted.wait(), timeout=5)
    await asyncio.wait_for(pool.close(), timeout=2)
    assert not pool._warmups


@pytest.mark.asyncio
async def test_control_run_manager_close_finishes_dispatched_run() -> None:
    from bianbu_agent.security.runtime_process.supervisor import ControlRunManager

    class Store:
        async def start(self, request_id, session_id, request): pass
        async def append(self, request_id, event): pass
        async def finish(self, request_id, result): pass
        async def get(self, request_id): return None
        async def list(self, session_id=None): return []

    entered = asyncio.Event()

    class Client:
        async def run_start(self, payload, on_event):
            entered.set()
            await asyncio.Event().wait()

        async def cancel(self, run_id):
            return True

    class Supervisor:
        ready = True
        client = Client()

    manager = ControlRunManager(Store(), Supervisor())
    handle = await manager.run("session", "hello", request_id="run-closing")
    await entered.wait()

    await manager.close()

    result = await handle.wait()
    assert result.status == "cancelled"
    assert result.metadata == {"reason": "control_shutdown"}

@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_edit', [False, True])
async def test_worker_refreshes_filesystem_capabilities_before_new_run(monkeypatch, invalid_edit):
    from unittest.mock import AsyncMock
    from bianbu_agent.security.runtime_process.worker import AgentWorker
    order = []
    handoffs = []
    async def reload(*, handoff: bool = False):
        handoffs.append(handoff)
        order.append('reload')
        if invalid_edit:
            from bianbu_agent.plugins.manager import CapabilityReloadError
            raise CapabilityReloadError('incomplete SKILL.md')
    handle = SimpleNamespace(wait=AsyncMock(return_value=None))
    async def run(*args, **kwargs):
        order.append('run')
        return handle
    worker = AgentWorker.__new__(AgentWorker)
    worker.runtime = SimpleNamespace(plugin_runtime=SimpleNamespace(reload_installed=reload),
                                     runs=SimpleNamespace(run=run))
    worker._activated = True
    worker._active = {}
    monkeypatch.setenv('BIANBU_WORKER_AGENT_ID', 'expert')
    await worker.dispatch('run.start', dict(run_id='new', session_id='s', thread_id='t',
                                          agent_id='expert', account_id='user', prompt='hello'))
    assert order == ['reload', 'run']
    # The Generation lease opens right after, so admission hands over the
    # Inventory it just verified instead of letting the lease walk it again.
    assert handoffs == [True]
    await asyncio.sleep(0)

@pytest.mark.asyncio
async def test_pool_catalog_reports_isolate_failure_and_do_not_start_workers():
    from unittest.mock import AsyncMock
    from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool
    pool = AgentWorkerPool(SimpleNamespace(agent_id='default'))
    idle_client = SimpleNamespace(catalog=AsyncMock())
    pool._supervisors = {
        'healthy': SimpleNamespace(ready=True, client=SimpleNamespace(catalog=AsyncMock(return_value={'mcps': ['weather']}))),
        'broken': SimpleNamespace(ready=True, client=SimpleNamespace(catalog=AsyncMock(side_effect=RuntimeError('offline')))),
        'idle': SimpleNamespace(ready=False, client=idle_client),
    }
    reports = await pool.catalogs()
    assert reports == [{'agent_id': 'healthy', 'catalog': {'mcps': ['weather']}},
                       {'agent_id': 'broken', 'error': 'offline'}]
    idle_client.catalog.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('managed', [False, True])
async def test_memory_registration_has_one_owner_and_standalone_renews(
    tmp_path, monkeypatch, managed,
):
    import json
    from bianbu_agent.server import memory_registry
    config_path = _write_config(tmp_path)
    await _prepare_runtime_data(config_path)
    config = bianbu_agent.load_runtime_config(config_path)
    root = tmp_path / 'memory-registry'
    root.mkdir()
    monkeypatch.setenv('BIANBU_AGENT_MEMORY_REGISTRY_DIR', str(root))
    monkeypatch.setenv('BIANBU_AGENT_MEMORY_REGISTRATION', '1')
    monkeypatch.setenv('BIANBU_WORKER_MANAGED', '1' if managed else '0')
    clock = [1_800_000_000.0]
    monkeypatch.setattr(memory_registry.time, 'time', lambda: clock[0])
    runtime = bianbu_agent.BianbuAgentRuntime(
        config=config, default_model=None, agent_id='default',
        model_resolver=YAMLModelResolver(config.data_dir / 'models'),
        agent_registry=InMemoryAgentRegistry((AgentSpec(id='default', name='Default', model=None),)),
        engine_builder=lambda *_: object(),
    )
    try:
        await runtime.start()
        if managed:
            assert list(root.iterdir()) == []  # Supervisor owns the host-PID registration.
        else:
            path = root / f'{os.getpid()}.json'
            original = json.loads(path.read_text())
            clock[0] += 25 * 3600
            # The already scheduled maintenance task observes the new clock.
            await asyncio.sleep(.01)
            assert json.loads(path.read_text())['expires_at_ms'] > original['expires_at_ms']
    finally:
        await runtime.close()
    assert list(root.iterdir()) == []
