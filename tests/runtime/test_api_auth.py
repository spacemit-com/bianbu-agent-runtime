from __future__ import annotations

import httpx
import pytest

import bianbu_agent


def _app(tmp_path):
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        "data_dir: .bianbu-agent\n",
        encoding="utf-8",
    )
    config = bianbu_agent.load_runtime_config(config_path)
    runtime = bianbu_agent.create_runtime(config, role="control")
    return bianbu_agent.create_runtime_app(config, manage_lifespan=False, role="control")


@pytest.mark.asyncio
async def test_admin_auth_is_disabled_in_packaged_production_mode(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_RUNTIME_ADMIN_TOKEN", "operator-secret")
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/executions")).status_code == 200
        session = await client.get("/api/v1/auth/session")
        assert session.json() == {
            "data": {"authenticated": True, "required": False}
        }
        exchange = await client.post(
            "/api/v1/auth/session", json={"token": "wrong-secret"}
        )
        assert exchange.json() == {
            "data": {"authenticated": True, "required": False}
        }
        assert "bianbu_admin_session=" not in exchange.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_production_keeps_session_prompt_endpoint_public(tmp_path, monkeypatch):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_RUNTIME_ADMIN_TOKEN", "operator-secret")
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/sessions/missing/prompt", json={})
    assert response.status_code != 401


@pytest.mark.asyncio
async def test_metrics_exposes_runtime_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "development")
    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    for name in (
        "runtime_control_up",
        "runtime_control_http_requests_total",
        "runtime_worker_instances",
        "runtime_worker_restarts_total",
        "runtime_security_prepare_total",
        "runtime_security_commit_total",
        "runtime_security_stop_total",
    ):
        assert name in response.text
