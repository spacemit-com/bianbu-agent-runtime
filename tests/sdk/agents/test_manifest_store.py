from __future__ import annotations

from pathlib import Path

import pytest

from bianbu_agent.agents import AgentSpec, ManifestAgentStore
from bianbu_agent.home import AgentHome


@pytest.mark.asyncio
async def test_manifest_store_crud_persists_packages_across_restart(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    store = ManifestAgentStore(home)
    await store.open()
    spec = AgentSpec(id="writer", name="Writer", tools=("*",))

    assert await store.create(spec) == spec
    assert await store.get("writer") == spec
    assert (home.agents_dir / "writer" / "agent.yaml").is_file()
    assert not (home.agents_dir / "writer.json").exists()

    updated = AgentSpec(id="writer", name="Senior Writer", skills=("review",))
    assert await store.update(updated) == updated
    assert [item.id for item in await store.list()] == ["writer"]
    await store.close()

    reopened = ManifestAgentStore(home)
    await reopened.open()
    assert await reopened.get("writer") == updated
    assert await reopened.delete("writer") is True
    assert await reopened.delete("writer") is False


@pytest.mark.asyncio
async def test_manifest_store_ignores_retired_agent_json_records(tmp_path: Path) -> None:
    """Agent JSON records are a retired format: they are never migrated."""

    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    home.agents_dir.mkdir(parents=True, exist_ok=True)
    (home.agents_dir / "writer.json").write_text(
        '{"schema_version": 1, "id": "writer", "name": "Writer"}\n',
        encoding="utf-8",
    )
    store = ManifestAgentStore(home)
    await store.open()

    assert await store.list() == []
    assert (home.agents_dir / "writer.json").is_file()
    assert not (home.cache_dir / "agent-json-migration").exists()
