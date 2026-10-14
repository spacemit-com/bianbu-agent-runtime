from __future__ import annotations

from pathlib import Path

from bianbu_agent._internal.json_records import JSONRecordDirectory
from bianbu_agent.agents import AgentSpec, agent_model_data, parse_agent_model


class JSONAgentStore:
    def __init__(self, root: Path) -> None:
        self._records = JSONRecordDirectory(root)

    async def open(self) -> None:
        await self._records.open()

    async def close(self) -> None:
        await self._records.close()

    async def get(self, agent_id: str) -> AgentSpec | None:
        async with self._records.lock:
            payload = self._records.read(agent_id)
            return None if payload is None else _decode(payload)

    async def list(self) -> list[AgentSpec]:
        async with self._records.lock:
            return [_decode(self._records.read(item) or {}) for item in self._records.list_ids()]

    async def create(self, spec: AgentSpec) -> AgentSpec:
        async with self._records.lock:
            self._records.create(spec.id, _encode(spec))
        return spec

    async def update(self, spec: AgentSpec) -> AgentSpec:
        async with self._records.lock:
            self._records.replace(spec.id, _encode(spec))
        return spec

    async def delete(self, agent_id: str) -> bool:
        async with self._records.lock:
            return self._records.delete(agent_id)


def _encode(spec: AgentSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "name_zh": spec.name_zh,
        "name_en": spec.name_en,
        "description": spec.description,
        "description_zh": spec.description_zh,
        "soul": spec.soul,
        "model": agent_model_data(spec.model),
        "tools": list(spec.tools),
        "skills": list(spec.skills),
        "mcps": list(spec.mcps),
        "plugins": list(spec.plugins),
        "permissions": dict(spec.permissions),
        "metadata": dict(spec.metadata),
    }


def _decode(payload: dict[str, object]) -> AgentSpec:
    return AgentSpec(
        id=str(payload["id"]),
        name=str(payload["name"]),
        name_zh=str(payload.get("name_zh", "")),
        name_en=str(payload.get("name_en", "")),
        description=str(payload.get("description", "")),
        description_zh=str(payload.get("description_zh", "")),
        soul=str(payload.get("soul", "")),
        model=parse_agent_model(payload.get("model")),
        tools=tuple(payload.get("tools", ())),
        skills=tuple(payload.get("skills", ())),
        mcps=tuple(payload.get("mcps", ())),
        plugins=tuple(payload["plugins"]) if "plugins" in payload else None,
        permissions=payload.get("permissions", {}),
        metadata=payload.get("metadata", {}),
    )


__all__ = ["JSONAgentStore"]
