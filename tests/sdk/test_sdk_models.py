from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from bianbu_agent import (
    AgentModel,
    CapabilityNotFoundError,
    ChatModel,
    ModelRecordIntegrityError,
    ModelRegistry,
    ModelResolutionError,
    OpenAICompatibleModel,
    YAMLModelResolver,
)
from bianbu_agent.models import ProviderConfig, YAMLModelStore, ModelConfig


@dataclass
class FakeModel:
    model: str


class Resolver:
    def resolve(self, model_id: str):
        return FakeModel("resolved:" + model_id) if model_id == "remote" else None

    def resolve_agent_model(self, selected: AgentModel):
        return FakeModel(f"{selected.provider}:{selected.name}")


def test_model_registry_resolves_structured_agent_model_without_default() -> None:
    registry = ModelRegistry(default_model=None, resolver=Resolver())

    resolved = registry.resolve(
        AgentModel(provider="openrouter", name="qwen/qwen3-235b")
    )

    assert resolved.model == "openrouter:qwen/qwen3-235b"


def test_model_registry_rejects_missing_agent_model_without_default() -> None:
    registry = ModelRegistry(default_model=None, resolver=Resolver())

    with pytest.raises(ModelResolutionError, match="agent_model_required"):
        registry.resolve(None)


@pytest.mark.asyncio
async def test_provider_store_resolves_agent_owned_remote_model(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.upsert_provider(
        ProviderConfig(
            id="openrouter",
            name="OpenRouter",
            protocol="openai-compatible",
            base_url="https://openrouter.ai/api/v1",
            api_key="secret",
        )
    )

    providers = await store.list_providers()
    resolved = YAMLModelResolver(tmp_path / "models").resolve_agent_model(
        AgentModel(provider="openrouter", name="qwen/qwen3-235b")
    )

    assert providers == [
        ProviderConfig(
            id="openrouter",
            name="OpenRouter",
            protocol="openai-compatible",
            base_url="https://openrouter.ai/api/v1",
            api_key="secret",
        )
    ]
    assert resolved.model == "qwen/qwen3-235b"
    assert resolved.provider_id == "openrouter"
    assert resolved.base_url == "https://openrouter.ai/api/v1"
    assert resolved.api_key == "secret"
    assert "secret" not in repr(resolved)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "parameters", "expected"),
    [
        # A model the device serves itself waits on its own compute, so it gets
        # the local floor instead of the network default that cut off a 4B
        # vision model mid-prefill on K3.
        ("http://127.0.0.1:18803/v1", {}, 900.0),
        ("http://localhost:11434/v1", {}, 900.0),
        ("http://[::1]:18803/v1", {}, 900.0),
        ("https://api.deepseek.com/v1", {}, 120.0),
        # An operator override is the explicit answer and wins over both.
        ("http://127.0.0.1:18803/v1", {"request_timeout_seconds": 45}, 45.0),
        ("https://api.deepseek.com/v1", {"request_timeout_seconds": 600}, 600.0),
    ],
)
async def test_agent_model_request_timeout_follows_the_provider_record(
    tmp_path: Path,
    base_url: str,
    parameters: dict[str, object],
    expected: float,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.upsert_provider(
        ProviderConfig(
            id="endpoint",
            name="Endpoint",
            protocol="openai-compatible",
            base_url=base_url,
            api_key="secret",
            parameters=parameters,
        )
    )

    resolved = YAMLModelResolver(tmp_path / "models").resolve_agent_model(
        AgentModel(provider="endpoint", name="some-model")
    )

    assert resolved.request_timeout_seconds == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [0, -1, 3601, float("inf"), float("nan"), "600", True],
)
async def test_agent_model_request_timeout_refuses_a_malformed_override(
    tmp_path: Path,
    value: object,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.upsert_provider(
        ProviderConfig(
            id="endpoint",
            name="Endpoint",
            protocol="openai-compatible",
            base_url="http://127.0.0.1:18803/v1",
            api_key="secret",
            parameters={"request_timeout_seconds": value},
        )
    )

    with pytest.raises(ValueError, match="request_timeout_seconds"):
        YAMLModelResolver(tmp_path / "models").resolve_agent_model(
            AgentModel(provider="endpoint", name="some-model")
        )


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        ("missing", "Provider credential record missing"),
    ],
)
def test_agent_model_resolution_rejects_missing_provider(
    tmp_path: Path,
    provider: str,
    message: str,
) -> None:
    with pytest.raises(ModelResolutionError, match=message):
        YAMLModelResolver(tmp_path / "models").resolve_agent_model(
            AgentModel(provider=provider, name="remote-model")
        )


@pytest.mark.asyncio
async def test_yaml_model_store_rejects_deleting_provider_used_by_model_resource(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await _create_custom_provider(store, "main")
    await store.create(ModelConfig(
        id="model-fast", provider="custom:main", model="fast",
    ))

    with pytest.raises(ModelRecordIntegrityError, match="model-fast"):
        await store.delete("custom-provider-main")


@pytest.mark.asyncio
async def test_yaml_model_store_rejects_invalidating_referenced_provider(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await _create_custom_provider(store, "main")
    await store.create(ModelConfig(
        id="model-fast", provider="custom:main", model="fast",
    ))

    with pytest.raises(ModelRecordIntegrityError, match="API key missing"):
        await store.update(ModelConfig(
            id="custom-provider-main",
            provider="custom:main",
            model="credential",
            base_url="https://models.example.test/v1",
            parameters={"record_kind": "custom_provider"},
        ))


def test_chat_model_is_framework_neutral_runtime_protocol() -> None:
    model = OpenAICompatibleModel(
        model="qwen3", api_key="secret", base_url="http://model.test/v1"
    )

    assert isinstance(model, ChatModel)
    assert model.model == "qwen3"
    assert "secret" not in repr(model)
    assert "langchain" not in str(ChatModel.__annotations__).lower()


def test_model_registry_applies_explicit_alias_resolver_and_default_precedence() -> None:
    default = FakeModel("default")
    fast = FakeModel("fast")
    registry = ModelRegistry(default_model=default, models={"fast": fast}, resolver=Resolver())

    explicit = FakeModel("explicit")
    assert registry.resolve(explicit) is explicit
    assert registry.resolve("fast") is fast
    with pytest.raises(ModelResolutionError, match="remote"):
        registry.resolve("remote")
    assert registry.resolve(None) is default
    with pytest.raises(ModelResolutionError, match="missing"):
        registry.resolve("missing")


@pytest.mark.asyncio
async def test_yaml_model_store_groups_provider_and_models_in_yaml_files(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.create(ModelConfig(
        id="provider-openai",
        provider="openai",
        model="credential",
        base_url="https://api.example.test/v1",
        parameters={"record_kind": "provider_credential", "api_key": "secret"},
    ))
    await store.create(ModelConfig(
        id="capability-chat",
        provider="openai",
        model="saved-chat",
        parameters={"capability": "chat"},
    ))
    registry = ModelRegistry(
        default_model=FakeModel("environment-default"),
        resolver=YAMLModelResolver(tmp_path / "models"),
    )

    selected = registry.resolve(None)
    explicit = YAMLModelResolver(tmp_path / "models").resolve_capability("chat")

    assert selected.model == "environment-default"
    assert explicit is not None
    assert explicit.model == "saved-chat"
    assert explicit.provider_id == "openai"
    assert explicit.base_url == "https://api.example.test/v1"
    assert explicit.api_key == "secret"
    provider_path = tmp_path / "models" / "openai" / "provider.yaml"
    model_path = tmp_path / "models" / "openai" / "saved-chat.yaml"
    assert provider_path.is_file()
    assert model_path.is_file()
    assert yaml.safe_load(provider_path.read_text(encoding="utf-8"))["id"] == (
        "provider-openai"
    )
    assert yaml.safe_load(model_path.read_text(encoding="utf-8"))["id"] == (
        "capability-chat"
    )
    assert not tuple((tmp_path / "models").rglob("*.json"))


@pytest.mark.asyncio
async def test_yaml_model_update_moves_to_the_new_model_filename(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    original = ModelConfig(
        id="model-chat",
        provider="openai",
        model="gpt-5",
    )
    await store.create(original)

    updated = ModelConfig(
        id=original.id,
        provider=original.provider,
        model="gpt-5-mini",
    )
    await store.update(updated)

    assert await store.get(original.id) == updated
    assert not (tmp_path / "models" / "openai" / "gpt-5.yaml").exists()
    assert (tmp_path / "models" / "openai" / "gpt-5-mini.yaml").is_file()


@pytest.mark.asyncio
async def test_yaml_model_move_journal_rolls_back_a_published_duplicate(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    original = ModelConfig(id="model-chat", provider="openai", model="gpt-5")
    await store.create(original)
    old_path = tmp_path / "models" / "openai" / "gpt-5.yaml"
    new_path = tmp_path / "models" / "openai" / "gpt-5-mini.yaml"
    document = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    document["model"] = "gpt-5-mini"
    store._records.user._begin_move(original.id, old_path, new_path)
    new_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    new_path.chmod(0o600)

    assert await store.get(original.id) == original
    assert old_path.is_file()
    assert not new_path.exists()
    assert not (tmp_path / "models" / ".model-record-move").exists()


@pytest.mark.asyncio
async def test_yaml_model_move_journal_rejects_cross_record_targets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    store = YAMLModelStore(root)
    await store.open()
    first = ModelConfig(id="model-a", provider="openai", model="a")
    second = ModelConfig(id="model-b", provider="openai", model="b")
    await store.create(first)
    await store.create(second)
    journal = root / ".model-record-move"
    journal.write_text(
        '{"schema_version": 1, "record_id": "model-a", '
        '"old": "openai/a.yaml", "new": "openai/b.yaml"}\n',
        encoding="utf-8",
    )
    journal.chmod(0o600)

    with pytest.raises(ModelResolutionError, match="invalid Model record move"):
        await store.get(first.id)

    assert (root / "openai" / "a.yaml").is_file()
    assert (root / "openai" / "b.yaml").is_file()


@pytest.mark.asyncio
async def test_yaml_model_move_journal_rejects_identical_targets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    store = YAMLModelStore(root)
    await store.open()
    config = ModelConfig(id="model-a", provider="openai", model="a")
    await store.create(config)
    journal = root / ".model-record-move"
    journal.write_text(
        '{"schema_version": 1, "record_id": "model-a", '
        '"old": "openai/a.yaml", "new": "openai/a.yaml"}\n',
        encoding="utf-8",
    )
    journal.chmod(0o600)

    with pytest.raises(ModelResolutionError, match="invalid Model record move"):
        await store.get(config.id)

    assert (root / "openai" / "a.yaml").is_file()


async def _create_custom_provider(
    store: YAMLModelStore,
    provider_id: str,
    *,
    api_key: str = "provider-secret",
    base_url: str = "https://models.example.test/v1",
) -> None:
    await store.create(ModelConfig(
        id=f"custom-provider-{provider_id}",
        provider=f"custom:{provider_id}",
        model="credential",
        base_url=base_url,
        parameters={
            "record_kind": "custom_provider",
            "custom_id": provider_id,
            "name": f"{provider_id.title()} Provider",
            "api_key": api_key,
        },
    ))


@pytest.mark.asyncio
async def test_json_model_resolver_resolves_custom_provider_chat_capability(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await _create_custom_provider(store, "chat")
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat"},
    ))

    resolved = YAMLModelResolver(tmp_path / "models").resolve_capability(
        "chat", required=True
    )

    assert resolved is not None
    assert resolved.model == "qwen-chat"
    assert resolved.provider_id == "custom:chat"
    assert resolved.api_key == "provider-secret"
    assert resolved.base_url == "https://models.example.test/v1"


@pytest.mark.asyncio
async def test_json_model_resolver_resolves_optional_vision_capability(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await _create_custom_provider(
        store,
        "vision",
        api_key="vision-secret",
        base_url="https://vision.example.test/v1",
    )
    await store.create(ModelConfig(
        id="capability-vision",
        provider="custom:vision",
        model="qwen-vision",
        parameters={"capability": "vision"},
    ))

    resolved = YAMLModelResolver(tmp_path / "models").resolve_capability("vision")

    assert resolved is not None
    assert resolved.model == "qwen-vision"
    assert resolved.api_key == "vision-secret"
    assert resolved.base_url == "https://vision.example.test/v1"


def test_json_model_resolver_returns_none_for_absent_optional_capability(
    tmp_path: Path,
) -> None:
    resolver = YAMLModelResolver(tmp_path / "models")

    assert resolver.resolve_capability("vision") is None


def test_model_registry_uses_injected_default_when_chat_capability_is_absent(
    tmp_path: Path,
) -> None:
    default = FakeModel("injected-default")
    registry = ModelRegistry(
        default_model=default,
        resolver=YAMLModelResolver(tmp_path / "models"),
    )

    assert registry.resolve(None) is default


def test_json_model_resolver_rejects_missing_required_chat_capability(
    tmp_path: Path,
) -> None:
    resolver = YAMLModelResolver(tmp_path / "models")

    with pytest.raises(CapabilityNotFoundError) as failure:
        resolver.resolve_capability("chat", required=True)

    assert "capability-chat" in str(failure.value)


@pytest.mark.asyncio
async def test_json_model_resolver_rejects_dangling_provider_reference_without_secrets(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await _create_custom_provider(store, "chat")
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat", "api_key": "must-not-leak"},
    ))
    (tmp_path / "models" / "custom-chat" / "provider.yaml").unlink()

    with pytest.raises(ModelResolutionError) as failure:
        YAMLModelResolver(tmp_path / "models").resolve_capability(
            "chat", required=True
        )

    message = str(failure.value)
    assert "capability-chat" in message
    assert "custom-provider-chat" in message
    assert "must-not-leak" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "base_url", "missing_type"),
    [
        ("", "https://models.example.test/v1", "API key"),
        (["not", "a", "secret"], "https://models.example.test/v1", "API key"),
        ("provider-secret", "", "Base URL"),
    ],
)
async def test_json_model_resolver_rejects_incomplete_provider(
    tmp_path: Path,
    api_key: object,
    base_url: str,
    missing_type: str,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await _create_custom_provider(store, "chat")
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat"},
    ))
    provider_path = tmp_path / "models" / "custom-chat" / "provider.yaml"
    provider_payload = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
    provider_payload["base_url"] = base_url
    provider_payload["parameters"]["api_key"] = api_key
    provider_path.write_text(
        yaml.safe_dump(provider_payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ModelResolutionError) as failure:
        YAMLModelResolver(tmp_path / "models").resolve_capability(
            "chat", required=True
        )

    message = str(failure.value)
    assert "custom-provider-chat" in message
    assert missing_type in message
    assert "provider-secret" not in message


@pytest.mark.asyncio
async def test_json_model_resolver_rejects_environment_provider_without_file_record(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.create(ModelConfig(
        id="provider-environment",
        provider="environment",
        model="credential",
        base_url="https://file-backed.example.test/v1",
        parameters={"record_kind": "provider_credential", "api_key": "file-secret"},
    ))
    await store.create(ModelConfig(
        id="capability-chat",
        provider="environment",
        model="legacy-chat",
        base_url="https://legacy.example.test/v1",
        parameters={"capability": "chat", "api_key": "legacy-secret"},
    ))
    (tmp_path / "models" / "environment" / "provider.yaml").unlink()

    with pytest.raises(ModelResolutionError) as failure:
        YAMLModelResolver(tmp_path / "models").resolve_capability(
            "chat", required=True
        )

    message = str(failure.value)
    assert "capability-chat" in message
    assert "provider-environment" in message
    assert "legacy-secret" not in message


@pytest.mark.asyncio
async def test_json_model_resolver_rejects_unsafe_provider_permissions(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await _create_custom_provider(store, "chat", api_key="permission-secret")
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat"},
    ))
    (tmp_path / "models" / "custom-chat" / "provider.yaml").chmod(0o644)

    with pytest.raises(ModelResolutionError) as failure:
        YAMLModelResolver(tmp_path / "models").resolve_capability(
            "chat", required=True
        )

    message = str(failure.value)
    assert "custom-provider-chat" in message
    assert "permissions" in message
    assert "permission-secret" not in message


@pytest.mark.asyncio
async def test_json_model_resolver_rejects_unsafe_capability_permissions_before_parsing(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await _create_custom_provider(store, "chat")
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat", "api_key": "capability-secret"},
    ))
    (tmp_path / "models" / "custom-chat" / "qwen-chat.yaml").chmod(0o644)

    with pytest.raises(ModelResolutionError) as failure:
        YAMLModelResolver(tmp_path / "models").resolve_capability(
            "chat", required=True
        )

    assert str(failure.value) == "Model record permissions unsafe: capability-chat"


@pytest.mark.asyncio
async def test_yaml_model_resolver_rejects_malformed_provider_yaml(
    tmp_path: Path,
) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await _create_custom_provider(store, "chat")
    await store.create(ModelConfig(
        id="capability-chat",
        provider="custom:chat",
        model="qwen-chat",
        parameters={"capability": "chat"},
    ))
    provider_path = tmp_path / "models" / "custom-chat" / "provider.yaml"
    provider_path.write_text('{"api_key":"malformed-secret"', encoding="utf-8")

    with pytest.raises(ValueError) as failure:
        YAMLModelResolver(tmp_path / "models").resolve_capability(
            "chat", required=True
        )

    assert "invalid Model YAML record" in str(failure.value)
    assert "malformed-secret" not in str(failure.value)
