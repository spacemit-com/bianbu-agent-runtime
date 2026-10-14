from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bianbu_agent import AgentModel, YAMLModelResolver
from bianbu_agent.models import ModelConfig, ProviderConfig, YAMLModelStore


def test_default_chat_model_metadata_and_entry_points_are_removed() -> None:
    with pytest.raises(TypeError):
        ModelConfig(
            id="model-chat",
            provider="chat",
            model="remote-chat",
            model_type="llm",
        )
    with pytest.raises(TypeError):
        ModelConfig(
            id="model-chat",
            provider="chat",
            model="remote-chat",
            is_default=True,
        )

    assert not hasattr(YAMLModelStore, "set_default")
    assert not hasattr(YAMLModelStore, "create_default_model")
    assert not hasattr(YAMLModelResolver, "resolve")
    assert not hasattr(YAMLModelResolver, "resolve_default")


@pytest.mark.asyncio
async def test_agent_resolution_needs_only_provider_record(tmp_path: Path) -> None:
    root = tmp_path / "models"
    store = YAMLModelStore(root)
    await store.open()
    await store.create_provider(
        ProviderConfig(
            id="chat",
            name="Chat",
            protocol="openai-compatible",
            base_url="https://chat.example.test/v1",
            api_key="secret",
        )
    )

    resolved = YAMLModelResolver(root).resolve_agent_model(
        AgentModel(provider="chat", name="remote-chat")
    )

    assert resolved.model == "remote-chat"
    assert resolved.base_url == "https://chat.example.test/v1"


@pytest.mark.asyncio
async def test_reopening_store_does_not_migrate_chat_capability_to_default(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    store = YAMLModelStore(root)
    await store.open()
    await store.create_project_capability(
        capability="chat",
        model="legacy-chat",
        base_url="https://chat.example.test/v1",
        api_key="secret",
    )
    await store.close()

    reopened = YAMLModelStore(root)
    await reopened.open()
    await reopened.close()

    record = yaml.safe_load(
        (root / "custom-chat" / "legacy-chat.yaml").read_text(encoding="utf-8")
    )
    assert "model_type" not in record
    assert "is_default" not in record
    assert record["id"] == "capability-chat"


@pytest.mark.asyncio
async def test_chat_capability_has_no_legacy_delete_protection(tmp_path: Path) -> None:
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    await store.create_project_capability(
        capability="chat",
        model="legacy-chat",
        base_url="https://chat.example.test/v1",
        api_key="secret",
    )

    deleted = await store.delete_capability_binding("chat")

    assert deleted is True
    assert await store.get("capability-chat") is None
    assert await store.get("custom-provider-chat") is None
