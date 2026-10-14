from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from bianbu_agent import AgentModel, AgentSpec, InMemoryAgentRegistry
from bianbu_agent.agents.stores import DirectoryAgentStore


def test_agent_spec_normalizes_capabilities_and_metadata() -> None:
    spec = AgentSpec(
        id="  researcher  ",
        name="  Researcher  ",
        name_zh="  研究专家  ",
        description="  Finds reliable answers.  ",
        soul="  Prefer primary sources.  ",
        model=AgentModel(
            provider=" openrouter ",
            name=" qwen/qwen3-235b ",
        ),
        tools=("web_search", "", "web_search", " read_file "),
        skills=("research", "research"),
        metadata={"avatar": "🔎"},
    )

    assert spec.id == "researcher"
    assert spec.name == "Researcher"
    assert spec.name_zh == "研究专家"
    assert spec.description == "Finds reliable answers."
    assert spec.soul == "Prefer primary sources."
    assert spec.model == AgentModel(
        provider="openrouter",
        name="qwen/qwen3-235b",
    )
    assert spec.tools == ("web_search", "read_file")
    assert spec.skills == ("research",)
    assert spec.metadata == {"avatar": "🔎"}


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"id": "", "name": "Agent"}, "id must not be empty"),
        ({"id": "agent", "name": ""}, "name must not be empty"),
    ],
)
def test_agent_spec_rejects_missing_identity(
    values: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        AgentSpec(**values)


@pytest.mark.asyncio
async def test_registry_crud_returns_agent_specs() -> None:
    registry = InMemoryAgentRegistry()
    created = await registry.create(
        AgentSpec(id="research", name="Research")
    )

    assert await registry.get("research") == created
    assert await registry.list() == [created]

    updated = replace(created, model="gpt-research")
    assert await registry.update(updated) == updated
    assert await registry.get("research") == updated
    assert await registry.delete("research") is True
    assert await registry.delete("research") is False
    assert await registry.get("research") is None


@pytest.mark.asyncio
async def test_registry_rejects_duplicate_create_and_missing_update() -> None:
    registry = InMemoryAgentRegistry()
    spec = AgentSpec(id="research", name="Research")
    await registry.create(spec)

    with pytest.raises(ValueError, match="agent already exists: research"):
        await registry.create(spec)
    with pytest.raises(KeyError, match="missing"):
        await registry.update(AgentSpec(id="missing", name="Missing"))


@pytest.mark.asyncio
async def test_registry_can_be_seeded_with_default_agent() -> None:
    default = AgentSpec(id="default", name="Default")
    registry = InMemoryAgentRegistry((default,))

    assert await registry.get("default") == default
    with pytest.raises(ValueError, match="unique"):
        InMemoryAgentRegistry((default, default))


def test_agent_spec_positional_metadata_uses_v05_field_order() -> None:
    metadata = {"avatar": "v05"}
    spec = AgentSpec("v05", "V05", "", "", "", "", "", None, (), (), (), metadata)

    assert dict(spec.metadata) == metadata
    assert spec.hooks == ()


def test_agent_spec_normalizes_display_fields() -> None:
    spec = AgentSpec(
        id="researcher",
        name="Researcher",
        name_zh="  研究专家  ",
        name_en="  Research Expert  ",
        description="  Finds answers.  ",
        description_zh="  找出答案。  ",
    )

    assert spec.name_zh == "研究专家"
    assert spec.name_en == "Research Expert"
    assert spec.description == "Finds answers."
    assert spec.description_zh == "找出答案。"


@pytest.mark.asyncio
async def test_directory_store_writes_safe_yaml_with_unicode_metadata(tmp_path) -> None:
    store = DirectoryAgentStore(tmp_path)
    spec = AgentSpec(
        id="research",
        name="Research",
        permissions={"level": "L2"},
        metadata={"avatar": "🔎", "language": "中文"},
    )

    await store.open()
    await store.create(spec)

    payload = yaml.safe_load((tmp_path / "research" / "agent.yaml").read_text(encoding="utf-8"))
    assert payload["metadata"] == {"avatar": "🔎", "language": "中文"}
    assert "hooks" not in payload
    assert payload["permissions"] == {"level": "L2"}


@pytest.mark.asyncio
async def test_directory_store_reads_ordinary_yaml_mapping_and_list_syntax(tmp_path) -> None:
    store = DirectoryAgentStore(tmp_path)
    agent_path = tmp_path / "research" / "agent.yaml"
    agent_path.parent.mkdir()
    agent_path.write_text(
        """\
id: research
name: Research
name_zh: 研究专家
description: Finds reliable answers.
soul: Prefer primary sources.
model: gpt-research
tools:
  - web_search
skills:
  - research
metadata:
  avatar: 🔎
""",
        encoding="utf-8",
    )

    assert await store.get("research") == AgentSpec(
        id="research",
        plugins=(),
        name="Research",
        name_zh="研究专家",
        description="Finds reliable answers.",
        soul="Prefer primary sources.",
        model="gpt-research",
        tools=("web_search",),
        skills=("research",),
        metadata={"avatar": "🔎"},
    )


@pytest.mark.asyncio
async def test_directory_store_normalizes_nested_mappings_and_sequences(
    tmp_path,
) -> None:
    store = DirectoryAgentStore(tmp_path)
    spec = AgentSpec(
        id="research",
        name="Research",
        metadata={
            "nested": OrderedDict(
                (
                    ("first", 1),
                    ("second", (OrderedDict((("label", "two"),)),)),
                )
            )
        },
    )

    await store.open()
    await store.create(spec)

    payload = yaml.safe_load(
        (tmp_path / "research" / "agent.yaml").read_text(encoding="utf-8")
    )
    assert payload["metadata"] == {
        "nested": {"first": 1, "second": [{"label": "two"}]}
    }


@pytest.mark.asyncio
async def test_directory_store_failed_create_cleans_up_and_allows_retry(
    tmp_path,
) -> None:
    store = DirectoryAgentStore(tmp_path)
    await store.open()

    with pytest.raises(yaml.representer.RepresenterError):
        await store.create(
            AgentSpec(
                id="research",
                name="Research",
                metadata={"unsupported": object()},
            )
        )

    assert not (tmp_path / "research").exists()
    assert list(tmp_path.rglob(".agent-*")) == []

    corrected = AgentSpec(
        id="research",
        name="Research",
        metadata={"supported": True},
    )
    assert await store.create(corrected) == corrected
    assert await store.get("research") == corrected


@pytest.mark.asyncio
async def test_directory_store_failed_create_preserves_nonempty_directory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DirectoryAgentStore(tmp_path)
    await store.open()
    from bianbu_agent.agents.stores import directory as store_module

    def fail_dump(payload, stream, **kwargs) -> None:
        del payload, kwargs
        package_root = Path(stream.name).parent
        (package_root / "user-asset.txt").write_text("keep", encoding="utf-8")
        stream.write("partial")
        raise RuntimeError("forced serialization failure")

    monkeypatch.setattr(store_module.yaml, "safe_dump", fail_dump)

    with pytest.raises(RuntimeError, match="forced serialization failure"):
        await store.create(AgentSpec(id="research", name="Research"))

    package_root = tmp_path / "research"
    assert (package_root / "user-asset.txt").read_text(encoding="utf-8") == "keep"
    assert list(package_root.glob(".agent-*")) == []
