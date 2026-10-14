from __future__ import annotations

from pathlib import Path

import pytest

from bianbu_agent import AgentSpec
from tests.sdk.api.test_agents import _api


@pytest.mark.asyncio
async def test_agent_crud_calls_logical_security_lifecycle(tmp_path: Path):
    async with _api(tmp_path) as (client, runtime):
        calls: list[tuple[str, str]] = []

        async def register(spec):
            calls.append(("register", spec.id))

        async def unregister(agent_id):
            calls.append(("unregister", str(agent_id)))

        runtime.register_logical_agent = register
        runtime.unregister_logical_agent = unregister

        provider = await client.post(
            "/api/v1/providers",
            json={"id": "lifecycle-provider", "label": "Lifecycle", "base_url": "https://model.test/v1", "api_key": "secret"},
        )
        assert provider.status_code == 201

        created = await client.post(
            "/api/v1/agents",
            json={"name": "Lifecycle", "model": {"provider": "lifecycle-provider", "name": "model"}},
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["data"]["id"]
        patched = await client.patch(f"/api/v1/agents/{agent_id}", json={"description": "updated"})
        removed = await client.delete(f"/api/v1/agents/{agent_id}")

    assert patched.status_code == 200
    assert removed.status_code == 204
    assert calls == [("register", agent_id), ("unregister", agent_id)]


@pytest.mark.asyncio
async def test_required_registration_failure_rolls_back_created_agent(tmp_path: Path):
    async with _api(tmp_path) as (client, runtime):
        runtime._security_runtime_client = type("Client", (), {"required": True})()

        async def fail(spec):
            raise RuntimeError("securityd unavailable")

        runtime.register_logical_agent = fail
        provider = await client.post(
            "/api/v1/providers",
            json={"id": "strict-provider", "label": "Strict", "base_url": "https://model.test/v1", "api_key": "secret"},
        )
        assert provider.status_code == 201
        with pytest.raises(RuntimeError, match="securityd unavailable"):
            await client.post(
                "/api/v1/agents",
                json={"name": "Strict", "model": {"provider": "strict-provider", "name": "model"}},
            )
        assert not any(
            agent.name == "Strict" for agent in await runtime._agent_store.list()
        )


@pytest.mark.asyncio
async def test_optional_security_unregister_pending_keeps_local_agent(tmp_path: Path):
    """Never delete Runtime state while securityd deletion is unconfirmed."""
    async with _api(tmp_path) as (client, runtime):
        await runtime.agents.create(AgentSpec(id="pending", name="Pending"))

        async def pending_unregister(agent_id):
            assert agent_id == "pending"
            return None

        runtime.unregister_logical_agent = pending_unregister
        runtime._security_runtime_client = type("Client", (), {"required": False})()
        runtime._security_registration_enabled = lambda: True

        response = await client.delete("/api/v1/agents/pending")

        assert response.status_code == 503
        assert await runtime.agents.get("pending") is not None
