from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from bianbu_agent.api import mcp_servers as mcp_servers_api
from bianbu_agent.mcp.client import MCPClient, MCPRemoteTool
from bianbu_agent.mcp.managed import parse_managed_mcp, write_managed_mcp_package
from bianbu_agent.plugins.models import PluginHealth
from bianbu_agent.server.app import create_app
from functools import partial
from tests.sdk.api.test_runs import _api as _run_api, _runtime as _run_runtime

# These MCP administration tests use an expert selecting every MCP. Exact
# Worker selection and the metadata-only Control API have separate tests.
_api = partial(_run_api, mcps=("*",))
_runtime = partial(_run_runtime, mcps=("*",))


@pytest.fixture(autouse=True)
def stub_persistent_transport(monkeypatch):
    # Transport I/O is injected through list_tools below; persistent session
    # ownership itself is tested with a real stdio server in test_client.py.
    async def start(_client):
        pass
    monkeypatch.setattr(MCPClient, "start", start)


MCP_PATH = "/api/v1/mcp-servers"
HEALTH_PATH = "/api/v1/mcp-health-checks"
NOW = datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc)
NOW_ISO = "2026-08-11T09:30:00Z"


@dataclass
class _Clock:
    value: float = 100.0

    def monotonic(self) -> float:
        return self.value


@pytest.fixture
async def mcp_health_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Exercise one real MCP generation while controlling only live health I/O."""

    package = tmp_path / ".bianbu-agent" / "mcps" / "mcp.deterministic"
    package.mkdir(parents=True)
    (package / "mcp.json").write_text(
        json.dumps(
            {
                "display_name": "Deterministic",
                "transport": "stdio",
                "command": "fake",
            }
        ),
        encoding="utf-8",
    )

    async def no_tools(_self: MCPClient):
        return ()

    monkeypatch.setattr(MCPClient, "list_tools", no_tools)
    runtime = await _runtime(tmp_path)
    gate = asyncio.Event()
    health_calls: list[int] = []

    async def controlled_health():
        generation = runtime.plugin_runtime.current_generation
        health_calls.append(generation.id)
        await gate.wait()
        return {
            server.runtime_id: PluginHealth.healthy()
            for server in generation.mcps.values()
        }

    monkeypatch.setattr(runtime.plugin_runtime, "health", controlled_health)
    app = create_app(runtime, manage_lifespan=False)
    cache = getattr(app.state, "mcp_health_cache", None)
    if cache is not None:
        clock = _Clock()
        cache.monotonic = clock.monotonic
        cache.utcnow = lambda: NOW
        runtime._test_mcp_health_clock = clock
        runtime._test_mcp_health_cache = cache
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            yield client, runtime, gate, health_calls
    finally:
        gate.set()
        if cache is not None:
            await cache.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_mcp_collection_returns_before_slow_health_check(mcp_health_api) -> None:
    client, runtime, _gate, health_calls = mcp_health_api

    response = await asyncio.wait_for(client.get(MCP_PATH), timeout=0.25)

    assert response.status_code == 200
    assert response.json()["data"][0]["health_checked_at"] is None
    assert health_calls == [runtime.plugin_runtime.current_generation.id]


@pytest.mark.asyncio
async def test_stale_reads_single_flight_one_background_refresh(mcp_health_api) -> None:
    client, runtime, _gate, health_calls = mcp_health_api

    first, second = await asyncio.wait_for(
        asyncio.gather(client.get(MCP_PATH), client.get(MCP_PATH)),
        timeout=0.25,
    )

    assert first.status_code == second.status_code == 200
    assert health_calls == [runtime.plugin_runtime.current_generation.id]


@pytest.mark.asyncio
async def test_manual_health_check_runs_after_forced_rediscovery(
    mcp_health_api,
) -> None:
    client, runtime, gate, health_calls = mcp_health_api
    automatic = await asyncio.wait_for(client.get(MCP_PATH), timeout=0.25)
    assert automatic.status_code == 200

    manual_task = asyncio.create_task(client.post(HEALTH_PATH))
    await asyncio.sleep(0)
    assert not manual_task.done()
    gate.set()
    manual = await manual_task

    assert manual.json()["data"][0]["health_checked_at"] == NOW_ISO
    assert health_calls == [
        runtime.plugin_runtime.current_generation.id,
        runtime.plugin_runtime.current_generation.id,
    ]


@pytest.mark.asyncio
async def test_manual_health_check_rediscovers_remote_mcp_tools(
    mcp_health_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit refresh atomically publishes changes from remote tools/list."""

    client, runtime, gate, _health_calls = mcp_health_api
    gate.set()
    old_generation_id = runtime.plugin_runtime.current_generation.id

    async def changed_tools(_self: MCPClient):
        return (
            MCPRemoteTool(
                "new_tool",
                "New tool",
                "Added remotely",
                {"type": "object"},
            ),
        )

    monkeypatch.setattr(MCPClient, "list_tools", changed_tools)

    refreshed = await client.post(HEALTH_PATH)

    assert refreshed.status_code == 200, refreshed.text
    assert runtime.plugin_runtime.current_generation.id != old_generation_id
    assert refreshed.json()["data"][0]["tools"] == [{
        "id": "mcp.deterministic/new_tool",
        "name": "new_tool",
        "display_name": "New tool",
    }]


@pytest.mark.asyncio
async def test_mcp_tool_selector_is_rejected_and_the_server_is_selectable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Agent names built-in Tools; an MCP Tool arrives with its server."""

    package = tmp_path / ".bianbu-agent" / "mcps" / "mcp.weather"
    package.mkdir(parents=True)
    (package / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": "fake"}),
        encoding="utf-8",
    )

    async def weather_tools(_self: MCPClient):
        return (
            MCPRemoteTool(
                "forecast",
                "Forecast",
                "Forecast weather",
                {"type": "object"},
            ),
        )

    monkeypatch.setattr(MCPClient, "list_tools", weather_tools)
    async with _api(tmp_path) as (client, _runtime_instance):
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "bound",
                    "base_url": "https://model.test/v1",
                    "api_key": "secret",
                },
            )
        ).status_code == 201
        rejected = await client.post(
            "/api/v1/agents",
            json={
                "name": "MCP Tool Agent",
                "model": {"provider": "bound", "name": "chat"},
                "tools": ["mcp.weather/forecast"],
            },
        )
        accepted = await client.post(
            "/api/v1/agents",
            json={
                "name": "MCP Server Agent",
                "model": {"provider": "bound", "name": "chat"},
                "mcps": ["mcp.weather"],
            },
        )

    assert rejected.status_code == 422, rejected.text
    assert "select the MCP server" in rejected.text
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["data"]["mcps"] == ["mcp.weather"]
    assert accepted.json()["data"]["available"] is True


@pytest.mark.asyncio
async def test_mcp_rollback_failure_preserves_worker_unavailable_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP cleanup failure cannot replace the Worker-unavailable response."""

    async with _api(tmp_path) as (client, runtime):
        async def worker_unavailable(_runtime):
            raise HTTPException(503, {"code": "worker_unavailable"})

        def rollback_failure(_mcp_id: str):
            raise OSError("synthetic rollback failure")

        monkeypatch.setattr(
            mcp_servers_api,
            "_reload_active_worker_bounded",
            worker_unavailable,
        )
        monkeypatch.setattr(runtime.mcps, "discard_install", rollback_failure)
        response = await client.post(
            MCP_PATH,
            json={"name": "Weather", "command": "fake"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "worker_unavailable"


@pytest.mark.asyncio
async def test_fresh_snapshot_is_reused_until_the_sixty_second_ttl(
    mcp_health_api,
) -> None:
    client, runtime, gate, health_calls = mcp_health_api
    gate.set()
    refreshed = await client.post(HEALTH_PATH)
    assert refreshed.status_code == 200
    generation_id = runtime.plugin_runtime.current_generation.id

    clock = runtime._test_mcp_health_clock
    clock.value += 59.999
    fresh = await client.get(MCP_PATH)
    assert fresh.json()["data"][0]["health_checked_at"] == NOW_ISO
    assert health_calls == [generation_id]

    clock.value += 0.001
    stale = await client.get(MCP_PATH)
    assert stale.status_code == 200
    await asyncio.sleep(0)
    assert health_calls == [generation_id, generation_id]


@pytest.mark.asyncio
async def test_manual_health_check_refreshes_even_a_fresh_snapshot(
    mcp_health_api,
) -> None:
    client, runtime, gate, health_calls = mcp_health_api
    gate.set()
    generation_id = runtime.plugin_runtime.current_generation.id

    first = await client.post(HEALTH_PATH)
    second = await client.post(HEALTH_PATH)

    assert first.status_code == second.status_code == 200
    assert health_calls == [generation_id, generation_id]


@pytest.mark.asyncio
async def test_generation_change_discards_snapshot_and_refreshes_new_generation(
    mcp_health_api,
    tmp_path: Path,
) -> None:
    client, runtime, gate, health_calls = mcp_health_api
    first = await client.get(MCP_PATH)
    assert first.status_code == 200
    old_generation_id = runtime.plugin_runtime.current_generation.id

    definition = parse_managed_mcp("Second", "fake")
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    package = write_managed_mcp_package(definition, candidate_root)
    runtime.mcps.install(package)
    await runtime.plugin_runtime.reload_installed()
    new_generation_id = runtime.plugin_runtime.current_generation.id
    assert new_generation_id != old_generation_id

    changed = await client.get(MCP_PATH)
    await asyncio.sleep(0)

    assert changed.status_code == 200
    assert {item["health_checked_at"] for item in changed.json()["data"]} == {None}
    assert health_calls == [old_generation_id, new_generation_id]

    gate.set()
    checked = await client.post(HEALTH_PATH)
    assert checked.status_code == 200
    assert {item["health_checked_at"] for item in checked.json()["data"]} == {
        NOW_ISO
    }
    assert health_calls == [old_generation_id, new_generation_id, new_generation_id]


@pytest.mark.asyncio
async def test_collection_renders_one_generation_when_publication_follows_snapshot_read(
    mcp_health_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime, gate, _health_calls = mcp_health_api
    gate.set()
    checked = await client.post(HEALTH_PATH)
    assert checked.status_code == 200

    candidate_root = tmp_path / "coherence-candidate"
    candidate_root.mkdir()
    package = write_managed_mcp_package(
        parse_managed_mcp("Published", "fake"),
        candidate_root,
    )
    cache = runtime._test_mcp_health_cache
    original_read = cache.read
    published = False

    async def publish_after_snapshot_read():
        nonlocal published
        snapshot = await original_read()
        if not published:
            published = True
            runtime.mcps.install(package)
            await runtime.plugin_runtime.reload_installed()
        return snapshot

    monkeypatch.setattr(cache, "read", publish_after_snapshot_read)

    response = await asyncio.wait_for(client.get(MCP_PATH), timeout=0.25)

    assert response.status_code == 200
    assert [server["id"] for server in response.json()["data"]] == [
        "mcp.deterministic",
        "mcp.published",
    ]
    assert {server["health_checked_at"] for server in response.json()["data"]} == {
        None
    }


@pytest.mark.asyncio
async def test_generation_refresh_waits_for_cancelled_health_cleanup(
    mcp_health_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime, _gate, health_calls = mcp_health_api
    old_generation_id = runtime.plugin_runtime.current_generation.id
    first_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_cleanup = asyncio.Event()
    active_checks = 0
    max_active_checks = 0

    async def health_with_delayed_cancellation():
        nonlocal active_checks, max_active_checks
        generation = runtime.plugin_runtime.current_generation
        health_calls.append(generation.id)
        active_checks += 1
        max_active_checks = max(max_active_checks, active_checks)
        try:
            if generation.id == old_generation_id:
                first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
                    await release_cleanup.wait()
                    raise
            return {
                server.runtime_id: PluginHealth.healthy()
                for server in generation.mcps.values()
            }
        finally:
            active_checks -= 1

    monkeypatch.setattr(
        runtime.plugin_runtime,
        "health",
        health_with_delayed_cancellation,
    )
    first = await client.get(MCP_PATH)
    assert first.status_code == 200
    await first_started.wait()

    candidate_root = tmp_path / "serialized-candidate"
    candidate_root.mkdir()
    package = write_managed_mcp_package(
        parse_managed_mcp("Serialized", "fake"),
        candidate_root,
    )
    runtime.mcps.install(package)
    await runtime.plugin_runtime.reload_installed()
    new_generation_id = runtime.plugin_runtime.current_generation.id

    changed = await asyncio.wait_for(client.get(MCP_PATH), timeout=0.25)
    await cancellation_seen.wait()

    assert changed.status_code == 200
    assert health_calls == [old_generation_id]
    assert max_active_checks == 1

    release_cleanup.set()
    refreshed = await client.post(HEALTH_PATH)
    assert refreshed.status_code == 200
    assert health_calls == [old_generation_id, new_generation_id, new_generation_id]
    assert max_active_checks == 1


@pytest.mark.asyncio
async def test_read_during_invalidation_waits_for_cancelled_health_cleanup(
    mcp_health_api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime, _gate, health_calls = mcp_health_api
    cache = runtime._test_mcp_health_cache
    old_generation_id = runtime.plugin_runtime.current_generation.id
    first_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_cleanup = asyncio.Event()
    new_refresh_started = asyncio.Event()
    release_new_refresh = asyncio.Event()
    active_checks = 0
    max_active_checks = 0

    async def health_with_delayed_cancellation():
        nonlocal active_checks, max_active_checks
        generation = runtime.plugin_runtime.current_generation
        health_calls.append(generation.id)
        active_checks += 1
        max_active_checks = max(max_active_checks, active_checks)
        try:
            if generation.id == old_generation_id:
                first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
                    await release_cleanup.wait()
                    raise
            new_refresh_started.set()
            await release_new_refresh.wait()
            return {
                server.runtime_id: PluginHealth.healthy()
                for server in generation.mcps.values()
            }
        finally:
            active_checks -= 1

    monkeypatch.setattr(
        runtime.plugin_runtime,
        "health",
        health_with_delayed_cancellation,
    )
    first = await client.get(MCP_PATH)
    assert first.status_code == 200
    await first_started.wait()

    candidate_root = tmp_path / "invalidation-candidate"
    candidate_root.mkdir()
    package = write_managed_mcp_package(
        parse_managed_mcp("Invalidated", "fake"),
        candidate_root,
    )
    runtime.mcps.install(package)
    await runtime.plugin_runtime.reload_installed()
    new_generation_id = runtime.plugin_runtime.current_generation.id

    invalidation = asyncio.create_task(cache.invalidate())
    await cancellation_seen.wait()
    changed = await asyncio.wait_for(client.get(MCP_PATH), timeout=0.25)
    await asyncio.sleep(0)

    assert changed.status_code == 200
    assert health_calls == [old_generation_id]
    assert max_active_checks == 1

    release_cleanup.set()
    await invalidation
    await new_refresh_started.wait()
    manual_refresh = asyncio.create_task(client.post(HEALTH_PATH))
    await asyncio.sleep(0)
    assert not manual_refresh.done()
    release_new_refresh.set()
    refreshed = await manual_refresh
    assert refreshed.status_code == 200
    assert health_calls == [old_generation_id, new_generation_id, new_generation_id]
    assert max_active_checks == 1


@pytest.mark.asyncio
async def test_failed_background_refresh_is_sanitized_and_retried(
    mcp_health_api,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, runtime, _gate, health_calls = mcp_health_api
    secret = "mcp-health-token=do-not-log"
    attempts = 0

    async def fail_once():
        nonlocal attempts
        generation = runtime.plugin_runtime.current_generation
        health_calls.append(generation.id)
        attempts += 1
        if attempts == 1:
            raise RuntimeError(secret)
        return {
            server.runtime_id: PluginHealth.healthy()
            for server in generation.mcps.values()
        }

    monkeypatch.setattr(runtime.plugin_runtime, "health", fail_once)
    with caplog.at_level("ERROR"):
        first = await client.get(MCP_PATH)
        await asyncio.sleep(0)
        second = await client.post(HEALTH_PATH)

    assert first.json()["data"][0]["health_checked_at"] is None
    assert second.status_code == 200
    assert second.json()["data"][0]["health_checked_at"] == NOW_ISO
    assert attempts == 2
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_health_check_rejects_every_non_empty_json_body(mcp_health_api) -> None:
    client, _runtime, gate, _health_calls = mcp_health_api
    gate.set()

    empty = await client.post(HEALTH_PATH)
    non_empty = [await client.post(HEALTH_PATH, json=value) for value in ({}, [])]
    non_empty.append(
        await client.post(
            HEALTH_PATH,
            content=b"null",
            headers={"content-type": "application/json"},
        )
    )
    non_empty.append(await client.post(HEALTH_PATH, json={"force": True}))

    assert empty.status_code == 200
    assert [response.status_code for response in non_empty] == [422, 422, 422, 422]


@pytest.mark.asyncio
async def test_install_and_delete_discard_health_from_previous_generations(
    mcp_health_api,
) -> None:
    client, runtime, gate, health_calls = mcp_health_api
    gate.set()
    initial = await client.post(HEALTH_PATH)
    assert initial.status_code == 200
    initial_generation_id = runtime.plugin_runtime.current_generation.id

    installed = await client.post(
        MCP_PATH,
        json={"name": "Mutable", "command": "fake"},
    )
    installed_generation_id = runtime.plugin_runtime.current_generation.id
    assert installed_generation_id != initial_generation_id
    assert installed.json()["data"]["health_checked_at"] is None

    checked = await client.post(HEALTH_PATH)
    assert checked.status_code == 200
    deleted = await client.delete(MCP_PATH, params={"id": "mcp.mutable"})
    deleted_generation_id = runtime.plugin_runtime.current_generation.id
    assert deleted.status_code == 204
    assert deleted_generation_id != installed_generation_id

    checked_after_delete = await client.post(HEALTH_PATH)
    assert checked_after_delete.status_code == 200
    assert health_calls == [
        initial_generation_id,
        installed_generation_id,
        installed_generation_id,
        deleted_generation_id,
    ]


@pytest.mark.asyncio
async def test_application_shutdown_cancels_refresh_before_runtime_close() -> None:
    events: list[str] = []
    refresh_started = asyncio.Event()

    class PluginRuntime:
        current_generation = type(
            "Generation",
            (),
            {
                "id": 7,
                "mcps": {
                    "mcp.blocked": type(
                        "Server",
                        (),
                        {"runtime_id": "mcp.blocked", "status": "healthy"},
                    )()
                },
            },
        )()

        async def health(self):
            events.append("health.start")
            refresh_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("health.cancelled")
                raise

    class Runtime:
        config = type("Config", (), {"language": "en"})()
        plugin_runtime = PluginRuntime()

        async def start(self) -> None:
            events.append("runtime.start")

        async def close(self) -> None:
            events.append("runtime.close")

    runtime = Runtime()
    app = create_app(runtime)

    async with app.router.lifespan_context(app):
        snapshot = await app.state.mcp_health_cache.read()
        assert snapshot.checked_at is None
        await refresh_started.wait()

    assert events == [
        "runtime.start",
        "health.start",
        "health.cancelled",
        "runtime.close",
    ]


@pytest.mark.asyncio
async def test_managed_mcp_server_resource_round_trips_with_normalized_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the versioned MCP resource must break managed server lifecycle."""

    async def list_tools(_self: MCPClient):
        return (MCPRemoteTool("read_file", "Read file", "", {"type": "object"}),)

    monkeypatch.setattr(MCPClient, "list_tools", list_tools)
    async with _api(tmp_path) as (client, runtime):
        request_started_at = datetime.now(timezone.utc)
        created = await client.post(
            "/api/v1/mcp-servers",
            json={
                "name": "Filesystem",
                "command": 'fake --root "/path with spaces"',
            },
        )

        assert created.status_code == 201
        server = created.json()["data"]
        assert server == {
            "id": "mcp.filesystem",
            "display_name": "Filesystem",
            "command": "fake --root '/path with spaces'",
            "kind": "mcp",
            "status": "healthy",
            # 生命周期字段：所有 MCP 界面共用的状态词、准备阶段、就绪标记和
            # 工具数，比只有一个 status 更能说明「现在到哪一步」。
            "state": "ready",
            "phase": "",
            "ready": True,
            "tools_count": 1,
            "transport": "stdio",
            "tools": [
                {
                    "id": "mcp.filesystem/read_file",
                    "name": "read_file",
                    "display_name": "Read file",
                }
            ],
            "source_kind": "standalone",
            "owner_plugin_id": None,
            "removable": True,
            "health_checked_at": None,
            "detail": "",
        }
        checked = await client.post(HEALTH_PATH)
        assert checked.status_code == 200
        expected_checked_at = checked.json()["data"][0]["health_checked_at"]
        listing = await client.get("/api/v1/mcp-servers")
        assert listing.status_code == 200
        listed_server = listing.json()["data"][0]
        assert listed_server["health_checked_at"] == expected_checked_at
        checked_at = datetime.fromisoformat(
            listed_server["health_checked_at"].replace("Z", "+00:00")
        )
        assert request_started_at <= checked_at <= datetime.now(timezone.utc)
        assert {
            key: value
            for key, value in listed_server.items()
            if key != "health_checked_at"
        } == {
            key: value for key, value in server.items() if key != "health_checked_at"
        }

        deleted = await client.delete(
            "/api/v1/mcp-servers", params={"id": "mcp.filesystem"}
        )
        assert deleted.status_code == 204
        assert not (runtime.home.mcps_dir / "mcp.filesystem").exists()


@pytest.mark.asyncio
async def test_referenced_mcp_server_is_retained_in_agent_after_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP deletion removes selection choices but preserves Agent metadata."""

    async def no_tools(_self: MCPClient):
        return ()

    monkeypatch.setattr(MCPClient, "list_tools", no_tools)
    async with _api(tmp_path) as (client, _):
        assert (
            await client.post(
                MCP_PATH,
                json={"name": "Bound MCP", "command": "fake"},
            )
        ).status_code == 201
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "bound",
                    "base_url": "https://model.test/v1",
                    "api_key": "secret",
                },
            )
        ).status_code == 201
        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "MCP Agent",
                "model": {"provider": "bound", "name": "chat"},
                "mcps": ["mcp.bound-mcp"],
            },
        )
        assert created.status_code == 201
        agent_id = created.json()["data"]["id"]

        deleted = await client.delete(MCP_PATH, params={"id": "mcp.bound-mcp"})
        detail = await client.get(f"/api/v1/agents/{agent_id}")

    assert deleted.status_code == 204
    assert detail.json()["data"]["mcps"] == ["mcp.bound-mcp"]
    assert detail.json()["data"]["available"] is True
    assert detail.json()["data"]["missing_powers"] == ["mcp:mcp.bound-mcp"]


@pytest.mark.asyncio
async def test_mcp_server_resource_is_strict_and_has_no_flat_alias(tmp_path: Path) -> None:
    """Accepting undeclared keys or `/api/mcp` would weaken the new boundary."""

    async with _api(tmp_path) as (client, _):
        unknown = await client.post(
            "/api/v1/mcp-servers",
            json={"name": "Bad", "command": "fake", "shell": True},
        )
        missing_id = await client.delete("/api/v1/mcp-servers")
        old_list = await client.get("/api/mcp")
        old_install = await client.post(
            "/api/mcp", json={"name": "Old", "command": "fake"}
        )

    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "validation_error"
    assert missing_id.status_code == 422
    assert [old_list.status_code, old_install.status_code] == [404, 404]


@pytest.mark.asyncio
async def test_managed_mcp_rejects_shell_syntax_and_duplicate_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_tools(_self: MCPClient):
        return ()

    monkeypatch.setattr(MCPClient, "list_tools", no_tools)
    async with _api(tmp_path) as (client, _):
        unsafe = await client.post(
            "/api/v1/mcp-servers",
            json={"name": "Unsafe", "command": "server && whoami"},
        )
        first = await client.post(
            "/api/v1/mcp-servers",
            json={"name": "Same Server", "command": "first"},
        )
        duplicate = await client.post(
            "/api/v1/mcp-servers",
            json={"name": "Same server", "command": "second"},
        )

    assert unsafe.status_code == 422
    assert unsafe.json()["error"]["code"] == "validation_error"
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "error": {
            "code": "mcp_already_installed",
            "message": "MCP server mcp.same-server is already installed",
            "details": {"mcp_id": "mcp.same-server"},
        }
    }


@pytest.mark.asyncio
async def test_managed_mcp_connection_failure_keeps_catalog_entry(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, runtime):
        response = await client.post(
            "/api/v1/mcp-servers",
            json={"name": "Broken", "command": "/missing/managed-mcp"},
        )

    assert response.status_code == 201
    assert response.json()["data"]["status"] == "unhealthy"
    # 没有这一行，坏掉的 Server 只会显示 unhealthy，用户无从知道原因。
    assert response.json()["data"]["detail"]
    assert (runtime.home.mcps_dir / "mcp.broken").exists()


@pytest.mark.asyncio
async def test_managed_mcp_delete_restores_package_when_reload_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_tools(_self: MCPClient):
        return ()

    monkeypatch.setattr(MCPClient, "list_tools", no_tools)
    async with _api(tmp_path) as (client, runtime):
        installed = await client.post(
            "/api/v1/mcp-servers",
            json={"name": "Stable", "command": "fake"},
        )
        assert installed.status_code == 201
        original_reload = runtime.plugin_runtime.reload_installed
        attempts = 0

        async def fail_once():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("candidate rejected")
            return await original_reload()

        monkeypatch.setattr(runtime.plugin_runtime, "reload_installed", fail_once)
        response = await client.delete(
            "/api/v1/mcp-servers",
            params={"id": "mcp.stable"},
        )

        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "mcp_reload_failed",
            "message": "MCP server mcp.stable could not be removed in the active runtime.",
            "details": {
                "mcp_id": "mcp.stable",
                "error_type": "RuntimeError",
                "rolled_back": True,
            },
        }
        assert attempts == 2
        assert (runtime.home.mcps_dir / "mcp.stable" / "mcp.json").is_file()
        assert "mcp.stable" in runtime.plugin_runtime.current_generation.mcps


@pytest.mark.asyncio
async def test_plugin_owned_mcp_is_visible_in_mcp_list_but_not_directly_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source" / "office.weather"
    member = source / "mcps" / "weather"
    member.mkdir(parents=True)
    (member / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": "fake"}),
        encoding="utf-8",
    )
    (source / "plugin.yaml").write_text(
        """\
schema_version: 1
name: office.weather
version: 1.0.0
""",
        encoding="utf-8",
    )

    async def list_tools(_self: MCPClient):
        return (MCPRemoteTool("forecast", "Forecast", "", {"type": "object"}),)

    monkeypatch.setattr(MCPClient, "list_tools", list_tools)
    async with _api(tmp_path / "runtime") as (client, runtime):
        installed = await client.post(
            "/api/v1/plugins",
            json={"source": str(source.resolve())},
        )
        assert installed.status_code == 201, installed.text
        listing = await client.get("/api/v1/mcp-servers")
        plugin_listing = await client.get("/api/v1/plugins")
        rejected = await client.delete(
            "/api/v1/mcp-servers",
            params={"id": "office.weather/weather"},
        )

        listing_data = listing.json()["data"]
        assert len(listing_data) == 1
        assert listing_data[0]["id"] == "office.weather/weather"
        assert listing_data[0]["owner_plugin_id"] == "office.weather"
        assert listing_data[0]["removable"] is False
        assert listing_data[0]["status"] == "healthy"
        assert listing_data[0]["tools"][0]["name"] == "forecast"
        members = plugin_listing.json()["data"][0]["members"]
        assert {"id": "office.weather/weather", "plugin_id": "office.weather", "kind": "mcp", "name": "weather", "version": "1.0.0"} in members
        assert rejected.status_code == 409
        assert rejected.json()["error"] == {
            "code": "mcp_owned_by_plugin",
            "message": (
                "MCP server office.weather/weather is managed by Plugin "
                "office.weather; remove the owning Plugin instead"
            ),
            "details": {
                "mcp_id": "office.weather/weather",
                "owner_plugin_id": "office.weather",
            },
        }
        assert "office.weather/weather" in runtime.plugin_runtime.current_generation.mcps


@pytest.mark.asyncio
async def test_mcp_activation_and_rollback_errors_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MCP lifecycle failures expose stable types and rollback state, never secrets."""

    activation_secret = "mcp-token=do-not-disclose"
    rollback_secret = "mcp-rollback-key=also-secret"
    async with _api(tmp_path) as (client, runtime):
        async def fail_activation():
            raise RuntimeError(activation_secret)

        def fail_rollback(_mcp_id: str) -> bool:
            raise OSError(rollback_secret)

        monkeypatch.setattr(runtime.plugin_runtime, "reload_installed", fail_activation)
        monkeypatch.setattr(runtime.mcps, "discard_install", fail_rollback)
        with caplog.at_level("ERROR"):
            response = await client.post(
                "/api/v1/mcp-servers",
                json={"name": "Secret", "command": "fake"},
            )

    rendered = response.text + caplog.text
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "mcp_activation_failed",
        "message": "MCP server mcp.secret could not be activated in the active runtime.",
        "details": {
            "mcp_id": "mcp.secret",
            "error_type": "RuntimeError",
            "rolled_back": False,
        },
    }
    assert activation_secret not in rendered
    assert rollback_secret not in rendered


@pytest.mark.asyncio
async def test_annotated_mcp_tools_are_projected_onto_the_resource_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server-declared annotations stay out of the MCP resource contract.

    The annotation flags drive the approval policy from the Worker ToolSpec
    metadata.  They used to ride along inside the tool view and made the
    listing fail validation for any server whose tools declared them.
    """

    async def annotated_tools(_self: MCPClient):
        return (
            MCPRemoteTool(
                "wipe_disk",
                "Wipe disk",
                "Destroy the disk",
                {"type": "object"},
                {
                    "read_only": False,
                    "destructive": True,
                    "idempotent": False,
                },
            ),
        )

    monkeypatch.setattr(MCPClient, "list_tools", annotated_tools)
    async with _api(tmp_path) as (client, runtime):
        created = await client.post(
            MCP_PATH,
            json={"name": "Annotated", "command": "fake"},
        )
        assert created.status_code == 201, created.text
        listed = await client.get("/api/v1/mcp-servers")
        assert listed.status_code == 200, listed.text
        assert listed.json()["data"][0]["tools"] == [{
            "id": "mcp.annotated/wipe_disk",
            "name": "wipe_disk",
            "display_name": "Wipe disk",
        }]
        assert listed.json()["data"][0]["status"] == "healthy"
