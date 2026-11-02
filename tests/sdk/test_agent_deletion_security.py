from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bianbu_agent import AgentSpec
from bianbu_agent.agents import AgentPackageManager
from bianbu_agent.home import AgentHome
from bianbu_agent.security import agent_deletion
from tests.sdk.api.test_agents import _file_backed_api


@pytest.fixture(autouse=True)
def _clear_security_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BIANBU_SECURITY",
        "BIANBU_SECURITYD_HOME",
        "BIANBU_SECURITY_CLIENT_DIR",
        "BIANBU_RUNTIME_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_security_deletion_stays_local_without_a_security_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_deletion, "_security_client_installed", lambda: False)

    assert agent_deletion.security_deletion_enabled() is False
    assert await agent_deletion.unregister_agent_before_delete("agent.demo") is False


@pytest.mark.asyncio
async def test_security_deletion_unregisters_and_rejects_unconfirmed_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Client:
        required = True

        async def _call(self, method: str, agent_id: str):
            calls.append(f"{method}:{agent_id}")
            return True

    import bianbu_agent.security.agent_registration as registration_module
    import bianbu_agent.security.security_runtime as security_runtime

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setattr(security_runtime, "SecurityRuntimeClient", lambda **_: Client())

    assert await agent_deletion.unregister_agent_before_delete("agent.demo") is True
    assert calls == ["unregister_agent:agent.demo"]

    class Pending(Client):
        async def _call(self, method: str, agent_id: str):
            return None

    class PendingRegistration:
        def __init__(self, _client) -> None:
            pass

        async def unregister(self, agent_id: str):
            return await Pending()._call("unregister_agent", agent_id)

    monkeypatch.setattr(
        registration_module, "SecurityAgentRegistration", PendingRegistration
    )
    with pytest.raises(agent_deletion.AgentSecurityUnregisterError, match="did not confirm"):
        await agent_deletion.unregister_agent_before_delete("agent.demo")


@pytest.mark.asyncio
async def test_security_deletion_skips_agents_without_the_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Plugin means no securityd identity, even on a security boundary."""

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setattr(
        agent_deletion,
        "security_deletion_enabled",
        lambda: True,
    )

    class ForbiddenClient:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("plain Agent deletion must not build a client")

    import bianbu_agent.security.security_runtime as security_runtime

    monkeypatch.setattr(
        security_runtime, "SecurityRuntimeClient", ForbiddenClient
    )
    assert (
        await agent_deletion.unregister_agent_before_delete(
            "agent.demo", plugins=()
        )
        is False
    )


@pytest.mark.asyncio
async def test_http_delete_unregisters_before_removing_the_declaration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _file_backed_api(tmp_path) as (client, runtime):
        await runtime.agents.create(
            AgentSpec(id="agent.demo", name="Demo", plugins=("bianbu.security",)),
            allow_capability_gap=True,
        )
        calls: list[str] = []

        async def unregister(agent_id: str):
            calls.append(agent_id)
            return True

        runtime._security_registration_enabled = lambda: True
        monkeypatch.setattr(runtime, "unregister_logical_agent", unregister)

        response = await client.delete("/api/v1/agents/agent.demo")

        assert response.status_code == 204, response.text
        assert calls == ["agent.demo"]
        with pytest.raises(KeyError):
            await runtime.agents.get("agent.demo")


@pytest.mark.asyncio
async def test_http_delete_keeps_the_declaration_when_unregister_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _file_backed_api(tmp_path) as (client, runtime):
        await runtime.agents.create(
            AgentSpec(id="agent.demo", name="Demo", plugins=("bianbu.security",)),
            allow_capability_gap=True,
        )

        async def reject(_agent_id: str):
            raise RuntimeError("securityd unavailable")

        runtime._security_registration_enabled = lambda: True
        monkeypatch.setattr(runtime, "unregister_logical_agent", reject)

        response = await client.delete("/api/v1/agents/agent.demo")

        assert response.status_code == 503
        assert await runtime.agents.get("agent.demo") is not None


@pytest.mark.asyncio
async def test_typed_package_delete_keeps_the_declaration_when_unregister_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _file_backed_api(tmp_path) as (client, runtime):
        spec = AgentSpec(id="agent.demo", name="Demo", plugins=("bianbu.security",))
        AgentPackageManager(runtime.home).create(spec)

        async def pending(_agent_id: str):
            return None

        runtime._security_registration_enabled = lambda: True
        monkeypatch.setattr(runtime, "unregister_logical_agent", pending)

        response = await client.delete(f"/api/v1/packages/agent/{spec.id}")

        assert response.status_code == 503, response.text
        assert runtime.agent_packages.inspect(spec.id) is not None


@pytest.mark.asyncio
async def test_cli_remove_unregisters_before_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from bianbu_agent.cli import main_async

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {"data_dir": str(tmp_path / "data"), "service": {"host": "127.0.0.1", "port": 18765}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert await main_async(
        [
            "--config", str(config), "agent", "create", "Demo",
            "--provider", "openai", "--model", "gpt-test",
            "--plugin", "bianbu.security",
        ]
    ) == 0
    agent_id = json.loads(capsys.readouterr().out)["id"]

    calls: list[tuple[str, object]] = []

    async def unregister(selected: str, *, plugins=None):
        calls.append((selected, plugins))
        return True

    monkeypatch.setattr(
        "bianbu_agent.security.agent_deletion.unregister_agent_before_delete",
        unregister,
    )
    assert await main_async(
        ["--config", str(config), "agent", "remove", agent_id]
    ) == 0
    assert calls == [(agent_id, ("bianbu.security",))]
    assert AgentPackageManager(AgentHome(tmp_path / "data")).inspect(agent_id) is None


@pytest.mark.asyncio
async def test_cli_remove_plain_agent_does_not_touch_securityd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from bianbu_agent.cli import main_async

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {"data_dir": str(tmp_path / "data"), "service": {"host": "127.0.0.1", "port": 18765}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert await main_async(
        [
            "--config", str(config), "agent", "create", "Demo",
            "--provider", "openai", "--model", "gpt-test",
        ]
    ) == 0
    agent_id = json.loads(capsys.readouterr().out)["id"]

    monkeypatch.setenv("BIANBU_SECURITY", "1")

    class ForbiddenClient:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("plain Agent removal must not contact securityd")

    import bianbu_agent.security.security_runtime as security_runtime

    monkeypatch.setattr(
        security_runtime, "SecurityRuntimeClient", ForbiddenClient
    )
    assert await main_async(
        ["--config", str(config), "agent", "remove", agent_id]
    ) == 0
    assert AgentPackageManager(AgentHome(tmp_path / "data")).inspect(agent_id) is None


@pytest.mark.asyncio
async def test_cli_remove_keeps_the_declaration_when_unregister_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from bianbu_agent.cli import main_async

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {"data_dir": str(tmp_path / "data"), "service": {"host": "127.0.0.1", "port": 18765}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert await main_async(
        [
            "--config", str(config), "agent", "create", "Demo",
            "--provider", "openai", "--model", "gpt-test",
        ]
    ) == 0
    agent_id = json.loads(capsys.readouterr().out)["id"]

    async def reject(_agent_id: str, *, plugins=None):
        raise agent_deletion.AgentSecurityUnregisterError("securityd unavailable")

    monkeypatch.setattr(
        "bianbu_agent.security.agent_deletion.unregister_agent_before_delete",
        reject,
    )
    with pytest.raises(agent_deletion.AgentSecurityUnregisterError):
        await main_async(
            ["--config", str(config), "agent", "remove", agent_id]
        )
    assert AgentPackageManager(AgentHome(tmp_path / "data")).inspect(agent_id) is not None
