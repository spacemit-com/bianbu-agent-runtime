from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import gzip
import json
from pathlib import Path
import time

import httpx
import pytest

from bianbu_agent import (
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    init_runtime_config,
    load_runtime_config,
)
from bianbu_agent.capture.llm import (
    MAX_LIST_RECORDS,
    MAX_RETAINED_RECORDS,
    MAX_RECORD_BYTES,
    MAX_STORAGE_BYTES,
    PREVIEW_BYTES,
    RETENTION_SECONDS,
)
from bianbu_agent.errors import ModelRecordIntegrityError, ModelRecordRollbackError
from bianbu_agent.models import ModelConfig, ProviderConfig
from bianbu_agent.server.app import create_app


@dataclass
class _Model:
    model: str


@asynccontextmanager
async def _operations_api(root: Path):
    config_path = root / ".bianbu-agent" / "config.yaml"
    if not config_path.exists():
        init_runtime_config(config_path)
    runtime = BianbuAgentRuntime(
        config=load_runtime_config(config_path),
        default_model=_Model("default"),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry((AgentSpec(id="default", name="Default"),)),
    )
    await runtime.start()
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client, runtime
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_settings_read_and_separate_deployment_runtime_patch_validation(
    tmp_path: Path,
) -> None:
    """Deployment and runtime stores must never be partially mixed or leak secrets."""

    async with _operations_api(tmp_path) as (client, runtime):
        initial = await client.get("/api/v1/settings")
        assert initial.status_code == 200
        assert initial.json()["data"]["deployment"]["language"] == "zh"

        deployment = await client.patch(
            "/api/v1/settings", json={"deployment": {"language": "en"}}
        )
        runtime_patch = await client.patch(
            "/api/v1/settings",
            json={"runtime": {"self_evolution_idle_minutes": 12, "self_evolution_enabled": True}},
        )
        mixed = await client.patch(
            "/api/v1/settings",
            json={
                "deployment": {"language": "zh"},
                "runtime": {"self_evolution_idle_minutes": 10},
            },
        )
        unknown = await client.patch(
            "/api/v1/settings", json={"runtime": {"api_key": "secret"}}
        )
        invalid = await client.patch(
            "/api/v1/settings", json={"runtime": {"self_evolution_idle_minutes": 0}}
        )
        unsupported_language = await client.patch(
            "/api/v1/settings", json={"deployment": {"language": "fr"}}
        )
        current = await client.get("/api/v1/settings")

        assert deployment.status_code == runtime_patch.status_code == 200
        assert (
            mixed.status_code
            == unknown.status_code
            == invalid.status_code
            == unsupported_language.status_code
            == 422
        )
        data = current.json()["data"]
        assert data["deployment"]["language"] == runtime.config.language == "en"
        assert data["runtime"]["self_evolution_idle_minutes"] == 12
        assert data["runtime"]["self_evolution_enabled"] is True
    assert "secret" not in json.dumps(data)


@pytest.mark.asyncio
async def test_persistence_failure_uses_sanitized_internal_error_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected store failures never fall through as plaintext or leak details."""

    async with _operations_api(tmp_path) as (client, runtime):
        async def failed_list() -> list[object]:
            raise RuntimeError("sqlite password=operator-secret")

        monkeypatch.setattr(runtime.sessions._store, "list", failed_list)
        response = await client.get("/api/v1/sessions?page=1&page_size=50")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error.",
            "details": {},
        }
    }
    assert "operator-secret" not in response.text


@pytest.mark.asyncio
async def test_log_snapshot_sse_reads_the_operational_records(
    tmp_path: Path,
) -> None:
    """Logs remain a finite SSE snapshot in the unified API.

    The Monitor → Logs page was empty while the Runtime only wrote the
    correlated JSONL under ``logs/operational`` and this endpoint globbed
    ``logs/*.log``.
    """

    async with _operations_api(tmp_path) as (client, runtime):
        log_path = (
            Path(runtime.config.data_dir) / "logs" / "operational" / "runtime.jsonl"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps({
                "timestamp": 1789725062.9997346,
                "level": "INFO",
                "logger": "bianbu_agent.test",
                "message": "first",
            }) + "\n"
            + json.dumps({
                "timestamp": 1789725063.5,
                "level": "ERROR",
                "logger": "bianbu_agent.test",
                "message": "second",
                "run_id": "run-7",
                "error_code": "provider_timeout",
            }) + "\n",
            encoding="utf-8",
        )
        logs = await client.get("/api/v1/logs")

    assert logs.headers["content-type"].startswith("text/event-stream")
    assert "[INFO] bianbu_agent.test: first" in logs.text
    # Level filters in the Console match this marker, and the correlation ids
    # travel with the line so a reader can tell which Run produced it.
    assert (
        "[ERROR] bianbu_agent.test: second run_id=run-7 "
        "error_code=provider_timeout"
    ) in logs.text


@pytest.mark.asyncio
async def test_provider_capture_resources_persist_validate_preview_detail_and_clear(
    tmp_path: Path,
) -> None:
    async with _operations_api(tmp_path) as (client, runtime):
        await runtime.model_configs.upsert_provider(
            ProviderConfig(
                id="custom:capture",
                name="Capture",
                protocol="openai-compatible",
                base_url="https://models.example.test/v1",
                api_key="secret",
            )
        )
        await runtime.model_configs.upsert_provider(
            ProviderConfig(
                id="custom:incomplete",
                name="Incomplete",
                protocol="openai-compatible",
                base_url="https://models.example.test/v1",
            )
        )
        missing = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "custom:missing"},
        )
        malformed = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "custom:capture", "unexpected": True},
        )
        surrogate = await client.put(
            "/api/v1/provider-capture",
            content=b'{"provider_id":"\\ud800"}',
            headers={"Content-Type": "application/json"},
        )
        incomplete = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "custom:incomplete"},
        )
        started = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "custom:capture"},
        )
        record_id = runtime.provider_capture_store.write(
            {
                "provider_id": "custom:capture",
                "model": "qwen",
                "status": "success",
                    "http_request": {"body": {"messages": ["request body"]}},
                    "http_response": {"body": {"text": "response body"}},
                "metadata": {
                    "agent_id": "agent-1",
                    "session_id": "session-1",
                    "run_id": "run-1",
                },
            }
        )
        collection = await client.get("/api/v1/provider-capture")
        detail = await client.get(f"/api/v1/provider-capture/{record_id}")
        invalid_detail = await client.get(
            "/api/v1/provider-capture/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        stopped = await client.delete("/api/v1/provider-capture/target")
        after_stop = await client.get("/api/v1/provider-capture")
        restarted_capture = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "custom:capture"},
        )
        cleared = await client.delete("/api/v1/provider-capture/records")
        stopped_again = await client.delete("/api/v1/provider-capture/target")

        assert missing.status_code == 404
        assert malformed.status_code == 422
        assert surrogate.status_code == 422
        assert incomplete.status_code == 422
        assert invalid_detail.status_code == 422
        assert started.status_code == 200
        assert started.json()["data"]["provider_id"] == "custom:capture"
        data = collection.json()["data"]
        assert data["provider_id"] == "custom:capture"
        assert data["limits"] == {
            "max_list_records": MAX_LIST_RECORDS,
            "max_preview_index_bytes": 64 * 1024,
            "max_record_bytes": MAX_RECORD_BYTES,
            "max_retained_records": MAX_RETAINED_RECORDS,
            "max_storage_bytes": MAX_STORAGE_BYTES,
            "preview_bytes": PREVIEW_BYTES,
            "retention_seconds": RETENTION_SECONDS,
        }
        assert len(data["records"]) == 1
        assert data["records"][0]["id"] == record_id
        assert "input" not in data["records"][0]
        assert "output" not in data["records"][0]
        assert data["records"][0]["input_preview"]["head"]
        assert detail.json()["data"]["http_request"]["body"] == {
            "messages": ["request body"]
        }
        assert stopped.json()["data"]["provider_id"] is None
        assert [item["id"] for item in after_stop.json()["data"]["records"]] == [
            record_id
        ]
        assert restarted_capture.json()["data"]["provider_id"] == "custom:capture"
        assert cleared.json()["data"]["provider_id"] == "custom:capture"
        assert cleared.json()["data"]["records"] == []
        assert stopped_again.json()["data"]["provider_id"] is None
        for response in (
            started,
            collection,
            detail,
            stopped,
            after_stop,
            restarted_capture,
            cleared,
            stopped_again,
        ):
            assert response.headers["cache-control"] == "no-store"
        for response in (missing, malformed, surrogate, incomplete, invalid_detail):
            assert response.headers["cache-control"] == "no-store"

    async with _operations_api(tmp_path) as (client, _runtime):
        restarted = await client.get("/api/v1/provider-capture")

    assert restarted.json()["data"]["provider_id"] is None
    assert restarted.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_provider_capture_filesystem_work_does_not_block_the_asgi_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling the synchronous store on-loop would stall unrelated requests/tasks."""

    async with _operations_api(tmp_path) as (client, runtime):
        original_list = runtime.provider_capture_store.list

        def slow_list(*args: object, **kwargs: object) -> list[dict[str, object]]:
            time.sleep(0.2)
            return original_list(*args, **kwargs)

        monkeypatch.setattr(runtime.provider_capture_store, "list", slow_list)
        started_at = asyncio.get_running_loop().time()
        request = asyncio.create_task(client.get("/api/v1/provider-capture"))
        await asyncio.sleep(0.02)
        heartbeat_elapsed = asyncio.get_running_loop().time() - started_at
        response = await request

    assert response.status_code == 200
    assert heartbeat_elapsed < 0.1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_value",
    [
        "NaN",
        "Infinity",
        "9" * 5_000,
        '"\\ud800"',
        ("[" * 200) + "0" + ("]" * 200),
    ],
    ids=("nan", "infinity", "huge-int", "surrogate", "deep"),
)
async def test_provider_capture_corrupt_json_is_hidden_and_detail_is_not_found(
    tmp_path: Path,
    invalid_value: str,
) -> None:
    record_id = "b" * 32
    async with _operations_api(tmp_path) as (client, runtime):
        records = runtime.provider_capture_store.records_root
        records.mkdir(parents=True, exist_ok=True)
        document = (
            f'{{"id":"{record_id}","captured_at":1,"input":{invalid_value}}}'
        ).encode("utf-8")
        (records / f"{record_id}.json.gz").write_bytes(gzip.compress(document))

        collection = await client.get("/api/v1/provider-capture")
        detail = await client.get(f"/api/v1/provider-capture/{record_id}")

    assert collection.status_code == 200
    assert collection.json()["data"]["records"] == []
    assert detail.status_code == 404
    assert detail.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_provider_capture_target_survives_runtime_restart_until_stopped(
    tmp_path: Path,
) -> None:
    async with _operations_api(tmp_path) as (client, runtime):
        await runtime.model_configs.upsert_provider(
            ProviderConfig(
                id="openrouter",
                name="OpenRouter",
                protocol="openai-compatible",
                base_url="https://openrouter.ai/api/v1",
                api_key="secret",
            )
        )
        await runtime.model_configs.upsert_provider(
            ProviderConfig(
                id="custom:switched",
                name="Switched",
                protocol="openai-compatible",
                base_url="https://models.example.test/v1",
                api_key="other-secret",
            )
        )
        response = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "openrouter"},
        )
        assert response.status_code == 200

    async with _operations_api(tmp_path) as (client, _runtime):
        persisted = await client.get("/api/v1/provider-capture")
        switched = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "custom:switched"},
        )

    assert persisted.json()["data"]["provider_id"] == "openrouter"
    assert switched.json()["data"]["provider_id"] == "custom:switched"

    async with _operations_api(tmp_path) as (client, _runtime):
        persisted_switch = await client.get("/api/v1/provider-capture")
        stopped = await client.delete("/api/v1/provider-capture/target")

    assert persisted_switch.json()["data"]["provider_id"] == "custom:switched"
    assert stopped.json()["data"]["provider_id"] is None


@pytest.mark.asyncio
async def test_provider_capture_rejects_non_provider_credential_record(
    tmp_path: Path,
) -> None:
    async with _operations_api(tmp_path) as (client, runtime):
        await runtime.model_configs.create(
            ModelConfig(
                id="provider-decoy",
                provider="decoy",
                model="credential",
                base_url="https://decoy.example.test/v1",
                parameters={
                    "record_kind": "not_a_provider",
                    "api_key": "decoy-secret",
                },
            )
        )

        providers = await client.get("/api/v1/providers")
        selected = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "decoy"},
        )

        assert all(item["id"] != "decoy" for item in providers.json()["data"])
        assert selected.status_code == 404
        assert selected.headers["cache-control"] == "no-store"
        assert runtime.provider_capture_store.target() is None


@pytest.mark.asyncio
async def test_provider_capture_rejects_whitespace_only_provider_api_key(
    tmp_path: Path,
) -> None:
    async with _operations_api(tmp_path) as (client, runtime):
        valid = await client.post(
            "/api/v1/providers",
            json={
                "id": "openai",
                "label": "OpenAI",
                "base_url": "https://api.openai.com/v1",
                "api_key": "valid-secret",
            },
        )
        assert valid.status_code == 201
        selected = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "openai"},
        )
        assert selected.status_code == 200

        whitespace = await client.post(
            "/api/v1/providers",
            json={
                "id": "openrouter",
                "label": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": " \t\n ",
            },
        )
        listed = await client.get("/api/v1/providers")
        rejected = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "openrouter"},
        )

        assert whitespace.status_code == 201
        stored_view = next(
            item for item in listed.json()["data"] if item["id"] == "openrouter"
        )
        assert stored_view["configured"] is False
        assert stored_view["api_key_masked"] == ""
        assert rejected.status_code == 422
        assert rejected.headers["cache-control"] == "no-store"
        assert runtime.provider_capture_store.target() == "openai"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            ModelRecordIntegrityError("Provider records are inconsistent"),
            409,
            "model_integrity_conflict",
        ),
        (
            ModelRecordRollbackError(("provider-openrouter",)),
            500,
            "model_rollback_failed",
        ),
    ],
)
async def test_provider_capture_recovery_errors_are_not_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    code: str,
) -> None:
    async with _operations_api(tmp_path) as (client, runtime):
        await runtime.model_configs.upsert_provider(
            ProviderConfig(
                id="openrouter",
                name="OpenRouter",
                protocol="openai-compatible",
                base_url="https://openrouter.ai/api/v1",
                api_key="secret",
            )
        )

        async def failed_catalog() -> list[ModelConfig]:
            raise error

        monkeypatch.setattr(runtime.model_configs, "list", failed_catalog)
        response = await client.put(
            "/api/v1/provider-capture",
            json={"provider_id": "openrouter"},
        )

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_all_legacy_administration_paths_are_absent(tmp_path: Path) -> None:
    """No flat management alias may survive the resource API migration."""

    paths = (
        "/api/config",
        "/api/upload",
        "/api/file?path=anything",
        "/api/agent-avatars",
        "/api/logs",
        "/api/tools",
        "/api/skills",
        "/api/hooks",
        "/api/agent-packages",
        "/api/plugins",
    )
    async with _operations_api(tmp_path) as (client, _):
        statuses = [(await client.get(path)).status_code for path in paths]

    assert statuses == [404] * len(paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [
    "agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps",
    "context_compression_threshold", "context_compression_target_ratio",
    "context_compression_protect_first_n", "context_compression_protect_last_n",
    "context_compression_abort_on_summary_failure",
])
async def test_automatic_context_settings_reject_obsolete_manual_controls(tmp_path, key):
    async with _operations_api(tmp_path) as (client, runtime):
        (runtime.config.data_dir / "config.json").write_text(json.dumps({key: 20}))
        response = await client.get('/api/v1/settings')
        assert response.status_code == 200, response.text
        assert key not in response.json()['data']['runtime']
        assert 'agent_max_context_tokens_explicit' not in response.json()['data']['runtime']
        response = await client.patch('/api/v1/settings', json={'runtime': {key: 20}})
        assert response.status_code == 422
