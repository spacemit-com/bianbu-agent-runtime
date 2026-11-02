"""The reconcile endpoint removes only orphaned securityd identities."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from bianbu_agent.api import security
from tests.sdk.api.test_agents import _api


@pytest.fixture
def securityd(monkeypatch):
    """A fake securityd that records unregister calls."""

    state = SimpleNamespace(registered=[], unregistered=[], fail_on=None)

    def invoke(operation, params):
        if operation == "agents.list.v1":
            return {"agents": [{"agent_id": agent_id} for agent_id in state.registered]}
        if operation == "agents.unregister.v1":
            agent_id = str(params["agent_id"])
            if agent_id == state.fail_on:
                raise RuntimeError("securityd unavailable")
            state.unregistered.append(agent_id)
            return {"ok": True}
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(security, "_invoke_sync", invoke)
    return state


async def _expert(client, name: str) -> str:
    provider = await client.post(
        "/api/v1/providers",
        json={"id": f"{name}-provider", "label": name,
              "base_url": "https://model.test/v1", "api_key": "secret"},
    )
    assert provider.status_code == 201, provider.text
    created = await client.post(
        "/api/v1/agents",
        json={"name": name, "model": {"provider": f"{name}-provider", "name": "model"}},
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"]


@pytest.mark.asyncio
async def test_reconcile_defaults_to_dry_run(tmp_path: Path, securityd) -> None:
    async with _api(tmp_path) as (client, runtime):
        agent_id = await _expert(client, "keeper")
        securityd.registered = [agent_id, "com.bianbu.stale", "com.bianbu.runtime"]
        response = await client.post("/api/v1/security/agents/reconcile", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["would_remove"] == ["com.bianbu.stale"]
    assert body["kept"] == [agent_id]
    assert body["skipped_service"] == ["com.bianbu.runtime"]
    assert securityd.unregistered == []


@pytest.mark.asyncio
async def test_reconcile_removes_orphans_only(tmp_path: Path, securityd) -> None:
    async with _api(tmp_path) as (client, runtime):
        agent_id = await _expert(client, "keeper")
        securityd.registered = [agent_id, "com.bianbu.stale", "com.bianbu.other"]
        response = await client.post(
            "/api/v1/security/agents/reconcile",
            json={"dry_run": False, "keep": ["com.bianbu.other"]},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is False
    assert body["removed"] == ["com.bianbu.stale"]
    assert body["kept"] == [agent_id, "com.bianbu.other"]
    assert securityd.unregistered == ["com.bianbu.stale"]


@pytest.mark.asyncio
async def test_reconcile_skips_an_agent_with_a_live_worker(
    tmp_path: Path, securityd,
) -> None:
    async with _api(tmp_path) as (client, runtime):
        original = getattr(runtime, "worker_supervisor", None)

        async def close():
            pass

        runtime.worker_supervisor = SimpleNamespace(
            busy_agents=lambda: ("com.bianbu.busy",),
            close=close,
        )
        try:
            securityd.registered = ["com.bianbu.busy", "com.bianbu.stale"]
            response = await client.post(
                "/api/v1/security/agents/reconcile", json={"dry_run": False},
            )
        finally:
            if original is None:
                del runtime.worker_supervisor
            else:
                runtime.worker_supervisor = original

    body = response.json()
    assert body["skipped_active"] == ["com.bianbu.busy"]
    assert body["removed"] == ["com.bianbu.stale"]
    assert securityd.unregistered == ["com.bianbu.stale"]


@pytest.mark.asyncio
async def test_reconcile_stops_at_the_first_unregister_failure(
    tmp_path: Path, securityd,
) -> None:
    securityd.fail_on = "com.bianbu.b"
    async with _api(tmp_path) as (client, runtime):
        securityd.registered = ["com.bianbu.a", "com.bianbu.b", "com.bianbu.c"]
        response = await client.post(
            "/api/v1/security/agents/reconcile", json={"dry_run": False},
        )

    body = response.json()
    assert body["removed"] == ["com.bianbu.a"]
    assert body["stopped"] is True
    assert [item["agent_id"] for item in body["failed"]] == ["com.bianbu.b"]
    assert "com.bianbu.c" not in securityd.unregistered


@pytest.mark.asyncio
async def test_reconcile_rejects_a_non_boolean_dry_run(tmp_path: Path, securityd) -> None:
    async with _api(tmp_path) as (client, runtime):
        response = await client.post(
            "/api/v1/security/agents/reconcile", json={"dry_run": "maybe"},
        )
    assert response.status_code == 400


def test_securityd_unregister_operation_is_wrapped_in_an_object(monkeypatch):
    class Client:
        def unregister_agent(self, agent_id):
            assert agent_id == "com.bianbu.stale"
            return True

    monkeypatch.setattr(
        security, "_call_client",
        lambda method, *args, **kwargs: getattr(Client(), method)(*args, **kwargs),
    )
    assert security._invoke_sync(
        "agents.unregister.v1", {"agent_id": "com.bianbu.stale"},
    ) == {"ok": True}


@pytest.mark.asyncio
async def test_reconcile_backs_up_the_registry_before_deleting(
    tmp_path: Path, securityd, monkeypatch,
) -> None:
    database = tmp_path / "capabilities.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE agent_manifest(agent_id TEXT)")
        connection.execute("INSERT INTO agent_manifest VALUES ('com.bianbu.stale')")
    monkeypatch.setenv("BIANBU_SECURITY_DB", str(database))
    monkeypatch.setenv("BIANBU_SECURITY_BACKUP_DIR", str(tmp_path / "backups"))

    async with _api(tmp_path) as (client, runtime):
        securityd.registered = ["com.bianbu.stale"]
        response = await client.post(
            "/api/v1/security/agents/reconcile", json={"dry_run": False},
        )

    body = response.json()
    assert body["removed"] == ["com.bianbu.stale"]
    backup = Path(body["backup"])
    assert (backup / "plan.json").is_file()
    with sqlite3.connect(backup / "capabilities.db") as connection:
        assert connection.execute(
            "SELECT agent_id FROM agent_manifest"
        ).fetchall() == [("com.bianbu.stale",)]


@pytest.mark.asyncio
async def test_a_failed_backup_aborts_before_any_deletion(
    tmp_path: Path, securityd, monkeypatch,
) -> None:
    database = tmp_path / "capabilities.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE agent_manifest(agent_id TEXT)")
    monkeypatch.setenv("BIANBU_SECURITY_DB", str(database))
    monkeypatch.setenv("BIANBU_SECURITY_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(
        security, "_backup_security_registry",
        lambda *_args: (_ for _ in ()).throw(
            security.HTTPException(503, "disk full")
        ),
    )

    async with _api(tmp_path) as (client, runtime):
        securityd.registered = ["com.bianbu.stale"]
        response = await client.post(
            "/api/v1/security/agents/reconcile", json={"dry_run": False},
        )

    assert response.status_code == 503
    assert securityd.unregistered == []


def test_boundary_preview_hides_retired_session_mounts() -> None:
    """The console must not promise a writable mount the Worker never gets.

    An older securityd still compiles ``sessions/checkpoints`` into the policy,
    but the Runtime drops every ``sessions/`` mount before launch because the
    control plane owns the transcript.  The preview has to match that launch
    policy instead of advertising a retired checkpointer directory.
    """

    view = {
        "mode": "sandbox",
        "agent_root": "/home/bianbu/.bianbu-agent",
        "read_only": [
            {"path": "/usr", "source": "system"},
            {"path": "<agent-root>", "source": "baseline"},
        ],
        "writable": [
            {"path": "<agent-root>/workspace", "source": "baseline"},
            {"path": "<agent-root>/sessions/checkpoints", "source": "baseline"},
        ],
        "read_only_overrides": [],
        "invisible": [
            {"path": "<agent-root>/sessions", "source": "declared"},
            {"path": "/root", "source": "baseline"},
        ],
    }

    filtered = security._drop_retired_session_boundary(view)

    assert [item["path"] for item in filtered["writable"]] == [
        "<agent-root>/workspace"
    ]
    assert [item["path"] for item in filtered["invisible"]] == ["/root"]
    # The original response is left alone; only the bridge copy is filtered.
    assert len(view["writable"]) == 2
