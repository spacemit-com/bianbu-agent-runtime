from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from bianbu_agent.api.models import (
    delete_provider_resource,
)
from bianbu_agent.models import ModelConfig
from tests.sdk.api.test_runs import _api


async def _store_model(
    runtime,
    model_id: str,
    provider_id: str,
    model: str,
    parameters: dict[str, object] | None = None,
):
    return await runtime.model_configs.create_model_resource(
        ModelConfig(
            id=model_id,
            provider=provider_id,
            model=model,
            parameters=parameters or {},
        ),
    )


@pytest.mark.asyncio
async def test_provider_crud_masks_secrets_and_preserves_omitted_patch_fields(
    tmp_path: Path,
) -> None:
    """Provider resources must own credentials without ever returning them raw."""

    async with _api(tmp_path) as (client, runtime):
        created = await client.post(
            "/api/v1/providers",
            json={
                "id": "openai",
                "label": "OpenAI",
                "base_url": "HTTPS://models.example.test:443/v1?token=leak#fragment",
                "api_key": "sk-provider-secret",
                "parameters": {"region": "test", "authorization": "also-secret"},
            },
        )

        assert created.status_code == 201
        provider = created.json()["data"]
        assert provider == {
            "id": "openai",
            "label": "OpenAI",
            "base_url": "https://models.example.test:443/v1",
            "configured": True,
            "api_key_masked": "sk-***********cret",
            # ``origin`` is what tells a client this Provider is the user's own
            # record; ``record_kind`` is ``custom_provider`` for packaged and
            # hand-made Providers alike.
            "origin": "user",
            "parameters": {"name": "OpenAI", "record_kind": "provider_credential"},
        }
        assert "sk-provider-secret" not in created.text
        assert "also-secret" not in created.text

        listed = await client.get("/api/v1/providers")
        fetched = await client.get("/api/v1/providers/openai")
        assert listed.status_code == fetched.status_code == 200
        assert listed.json()["data"] == [provider]
        assert fetched.json()["data"] == provider

        patched = await client.patch(
            "/api/v1/providers/openai",
            json={"label": "Primary"},
        )
        assert patched.status_code == 200
        assert patched.json()["data"]["label"] == "Primary"
        assert patched.json()["data"]["base_url"] == provider["base_url"]
        assert patched.json()["data"]["api_key_masked"] == provider["api_key_masked"]
        parameters_only = await client.patch(
            "/api/v1/providers/openai",
            json={"parameters": {"region": "backup"}},
        )
        assert parameters_only.status_code == 200
        assert parameters_only.json()["data"]["label"] == "Primary"
        assert parameters_only.json()["data"]["base_url"] == provider["base_url"]
        assert parameters_only.json()["data"]["api_key_masked"] == (
            provider["api_key_masked"]
        )
        stored = await runtime.model_configs.get("provider-openai")
        assert stored is not None
        assert stored.parameters["api_key"] == "sk-provider-secret"

        deleted = await client.delete("/api/v1/providers/openai")
        missing = await client.get("/api/v1/providers/openai")
        assert deleted.status_code == 204
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_packaged_provider_is_deletable_without_touching_the_package(
    tmp_path: Path, monkeypatch
) -> None:
    """The factory credential can be removed; the signed package keeps its bytes."""

    system_root = tmp_path / "system"
    provider_dir = system_root / "models" / "custom-bibit"
    provider_dir.mkdir(parents=True)
    provider_path = provider_dir / "provider.yaml"
    provider_path.write_text(
        "schema_version: 1\n"
        "provider: custom:bibit\n"
        "model: credential\n"
        "base_url: https://api.deepseek.com/v1\n"
        "parameters:\n"
        "  record_kind: custom_provider\n"
        "  custom_id: bibit\n"
        "  name: Bibit Built-in\n"
        "  api_key: sk-fixture\n"
        "id: custom-provider-bibit\n",
        encoding="utf-8",
    )
    provider_path.chmod(0o644)
    original = provider_path.read_bytes()
    monkeypatch.setenv("BIANBU_SYSTEM_RESOURCE_ROOT", str(system_root))

    async with _api(tmp_path) as (client, runtime):
        listed = await client.get("/api/v1/providers")
        assert [item["id"] for item in listed.json()["data"]] == ["custom:bibit"]
        assert listed.json()["data"][0]["origin"] == "system"

        # ``origin`` answers "does the package ship this record?", not "which
        # layer holds the bytes?".  Editing a packaged Provider writes a user
        # copy that shadows the packaged one, but the Provider is still the
        # factory one: deleting it hides the shipped record, and a reinstall
        # (or re-creating the same ID) brings it back.  Clients keying the
        # "factory provider" notice off ``record_kind`` cannot know this.
        patched = await client.patch(
            "/api/v1/providers/custom:bibit",
            json={"label": "Bibit Built-in"},
        )
        assert patched.status_code == 200
        assert patched.json()["data"]["origin"] == "system"
        assert (await client.get("/api/v1/providers")).json()["data"][0]["origin"] == "system"

        deleted = await client.delete("/api/v1/providers/custom:bibit")

        assert deleted.status_code == 204, deleted.text
        assert (await client.get("/api/v1/providers")).json()["data"] == []

    assert provider_path.read_bytes() == original
    marker = runtime.model_configs._records.root / ".hidden" / "custom-provider-bibit"
    assert marker.is_file()
    assert marker.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_provider_reference_and_project_pair_rollback_conflicts_are_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Referenced Providers conflict, and failed pair PATCH restores both records."""

    async with _api(tmp_path) as (client, runtime):
        await runtime.model_configs.create_project_capability(
            capability="tts",
            model="old-tts",
            base_url="https://old.example.test/v1",
            api_key="old-provider-secret",
            voice="old-voice",
        )
        original_replace = runtime.model_configs._records.user.replace
        provider_calls = 0

        def fail_primary_and_rollback(record_id, payload):
            nonlocal provider_calls
            if str(record_id) == "custom-provider-tts":
                provider_calls += 1
                if provider_calls == 1:
                    original_replace(record_id, payload)
                    raise OSError("new-provider-secret primary write failure")
                raise OSError("old-provider-secret rollback failure")
            return original_replace(record_id, payload)

        monkeypatch.setattr(
            runtime.model_configs._records.user,
            "replace",
            fail_primary_and_rollback,
        )
        transport = httpx.ASGITransport(
            app=client._transport.app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as unsafe:
            failed = await unsafe.patch(
                "/api/v1/providers/custom:tts",
                json={"base_url": "https://new.example.test/v1"},
            )

        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "model_rollback_failed"
        assert failed.json()["error"]["details"]["record_ids"] == [
            "custom-provider-tts"
        ]
        assert "provider-secret" not in failed.text


@pytest.mark.asyncio
async def test_project_provider_patch_rolls_back_every_field_as_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed final Provider snapshot cannot leave PATCH fields committed."""

    async with _api(tmp_path) as (client, runtime):
        before_provider, before_capability = (
            await runtime.model_configs.create_project_capability(
                capability="tts",
                model="old-tts",
                base_url="https://old.example.test/v1",
                api_key="old-provider-secret",
                voice="old-voice",
            )
        )
        original_replace = runtime.model_configs._records.user.replace
        failed_once = False

        def fail_after_final_provider_write(record_id, payload):
            nonlocal failed_once
            parameters = payload.get("parameters", {})
            if (
                str(record_id) == before_provider.id
                and isinstance(parameters, dict)
                and parameters.get("name") == "New TTS"
                and not failed_once
            ):
                failed_once = True
                original_replace(record_id, payload)
                raise OSError("injected final Provider write failure")
            return original_replace(record_id, payload)

        monkeypatch.setattr(
            runtime.model_configs._records.user,
            "replace",
            fail_after_final_provider_write,
        )
        transport = httpx.ASGITransport(
            app=client._transport.app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as unsafe:
            response = await unsafe.patch(
                "/api/v1/providers/custom:tts",
                json={
                    "label": "New TTS",
                    "base_url": "https://new.example.test/v2",
                    "api_key": "new-provider-secret",
                    "parameters": {"region": "new"},
                },
            )

        assert response.status_code == 500
        assert await runtime.model_configs.get(before_provider.id) == before_provider
        assert await runtime.model_configs.get(before_capability.id) == before_capability
        assert not (runtime.home.models_dir / ".model-records.transaction").exists()
        assert "provider-secret" not in response.text


@pytest.mark.asyncio
async def test_model_resources_reject_action_discriminators_and_old_paths(
    tmp_path: Path,
) -> None:
    """The resource API must not retain the old Model action bus or flat routes."""

    async with _api(tmp_path) as (client, _):
        rejected = (
            await client.post(
                "/api/v1/providers",
                json={"id": "openai", "action": "set_provider"},
            ),
        )
        removed_paths = (
            await client.get("/api/v1/models"),
            await client.post("/api/v1/models", json={}),
            await client.get("/api/v1/capability-bindings"),
            await client.put("/api/v1/capability-bindings/chat", json={}),
            await client.delete("/api/v1/capability-bindings/chat"),
            await client.get("/api/models"),
            await client.post("/api/models", json={"action": "set_provider"}),
            await client.put("/api/models/m", json={"model": "legacy"}),
            await client.delete("/api/models/m"),
        )

    assert all(response.status_code == 422 for response in rejected)
    assert all(response.status_code == 404 for response in removed_paths)
    for response in rejected:
        error = response.json()["error"]["details"]["errors"][0]
        assert error["loc"][-1] == "action"
        assert error["type"] == "extra_forbidden"


@pytest.mark.asyncio
async def test_provider_delete_and_model_create_are_one_locked_decision(
    tmp_path: Path,
) -> None:
    """A queued Provider delete cannot race a validated Model create."""

    async with _api(tmp_path) as (_, runtime):
        await runtime.model_configs.create(
            ModelConfig(
                id="provider-race",
                provider="race",
                model="credential",
                base_url="https://race.example.test/v1",
                parameters={
                    "record_kind": "provider_credential",
                    "api_key": "race-secret",
                },
            )
        )
        await runtime.model_configs._records.lock.acquire()
        try:
            delete_task = asyncio.create_task(
                delete_provider_resource(runtime.model_configs, "race")
            )
            await asyncio.sleep(0)
            create_task = asyncio.create_task(
                runtime.model_configs.create_model_resource(
                    ModelConfig(
                        id="race-model",
                        provider="race",
                        model="race-model",
                    ),
                )
            )
            await asyncio.sleep(0)
        finally:
            runtime.model_configs._records.lock.release()

        deleted, created = await asyncio.gather(
            delete_task,
            create_task,
            return_exceptions=True,
        )
        provider = await runtime.model_configs.get("provider-race")
        model = await runtime.model_configs.get("race-model")

        assert sum(isinstance(item, Exception) for item in (deleted, created)) == 1
        assert not (model is not None and provider is None)


@pytest.mark.asyncio
async def test_provider_delete_and_agent_create_are_one_catalog_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent creation cannot pass validation while its Provider is being deleted."""

    async with _api(tmp_path) as (client, runtime):
        created = await client.post(
            "/api/v1/providers",
            json={
                "id": "agent-race",
                "base_url": "https://race.example.test/v1",
                "api_key": "race-secret",
            },
        )
        assert created.status_code == 201
        entered_delete = asyncio.Event()
        release_delete = asyncio.Event()
        original_delete = runtime.model_configs.delete_provider_resource

        async def delayed_delete(record_id: str) -> bool:
            entered_delete.set()
            await release_delete.wait()
            return await original_delete(record_id)

        monkeypatch.setattr(
            runtime.model_configs,
            "delete_provider_resource",
            delayed_delete,
        )
        delete_task = asyncio.create_task(
            client.delete("/api/v1/providers/agent-race")
        )
        await asyncio.wait_for(entered_delete.wait(), timeout=1)
        create_task = asyncio.create_task(
            client.post(
                "/api/v1/agents",
                json={
                    "name": "Racing Agent",
                    "model": {
                        "provider": "agent-race",
                        "name": "chat",
                    },
                },
            )
        )
        await asyncio.sleep(0)
        release_delete.set()
        deleted, agent = await asyncio.gather(delete_task, create_task)
        provider = await runtime.model_configs.get_provider("agent-race")
        stored_agent = next(
            (
                item
                for item in await runtime._agent_store.list()
                if item.name == "Racing Agent"
            ),
            None,
        )

    assert sorted((deleted.status_code, agent.status_code)) == [204, 422]
    assert not (provider is None and stored_agent is not None)


@pytest.mark.asyncio
async def test_model_delete_and_binding_put_are_one_locked_decision(
    tmp_path: Path,
) -> None:
    """A queued Model delete cannot race a validated binding PUT."""

    async with _api(tmp_path) as (client, runtime):
        await client.post(
            "/api/v1/providers",
            json={
                "id": "openai",
                "base_url": "https://race.example.test/v1",
                "api_key": "race-secret",
            },
        )
        await _store_model(runtime, "race-model", "openai", "race")
        await runtime.model_configs._records.lock.acquire()
        try:
            delete_task = asyncio.create_task(
                runtime.model_configs.delete_model_resource("race-model")
            )
            await asyncio.sleep(0)
            bind_task = asyncio.create_task(
                runtime.model_configs.upsert_capability_binding(
                    capability="vision",
                    model_id="race-model",
                    options={},
                )
            )
            await asyncio.sleep(0)
        finally:
            runtime.model_configs._records.lock.release()

        deleted, bound = await asyncio.gather(
            delete_task,
            bind_task,
            return_exceptions=True,
        )
        model = await runtime.model_configs.get("race-model")
        binding = await runtime.model_configs.get("capability-vision")

        assert sum(isinstance(item, Exception) for item in (deleted, bound)) == 1
        assert not (binding is not None and model is None)
