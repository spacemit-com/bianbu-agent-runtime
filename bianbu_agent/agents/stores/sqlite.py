from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from bianbu_agent.agents import AgentSpec, agent_model_data, parse_agent_model


class SQLiteAgentStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path).expanduser().resolve()
        self._connection: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._connection is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path)
        connection.row_factory = aiosqlite.Row
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sdk_agents (
                id TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL
            )
            """
        )
        await connection.commit()
        self._connection = connection

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    def _opened(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Agent store is not open")
        return self._connection

    async def get(self, agent_id: str) -> AgentSpec | None:
        cursor = await self._opened().execute(
            "SELECT spec_json FROM sdk_agents WHERE id = ?", (str(agent_id).strip(),)
        )
        row = await cursor.fetchone()
        return None if row is None else _decode(row["spec_json"])

    async def list(self) -> list[AgentSpec]:
        cursor = await self._opened().execute(
            "SELECT spec_json FROM sdk_agents ORDER BY id"
        )
        return [_decode(row["spec_json"]) for row in await cursor.fetchall()]

    async def create(self, spec: AgentSpec) -> AgentSpec:
        try:
            await self._opened().execute(
                "INSERT INTO sdk_agents(id, spec_json) VALUES (?, ?)",
                (spec.id, _encode(spec)),
            )
            await self._opened().commit()
        except aiosqlite.IntegrityError as exc:
            raise ValueError(f"agent already exists: {spec.id}") from exc
        return spec

    async def update(self, spec: AgentSpec) -> AgentSpec:
        cursor = await self._opened().execute(
            "UPDATE sdk_agents SET spec_json = ? WHERE id = ?",
            (_encode(spec), spec.id),
        )
        if cursor.rowcount != 1:
            await self._opened().rollback()
            raise KeyError(spec.id)
        await self._opened().commit()
        return spec

    async def delete(self, agent_id: str) -> bool:
        cursor = await self._opened().execute(
            "DELETE FROM sdk_agents WHERE id = ?", (str(agent_id).strip(),)
        )
        await self._opened().commit()
        return cursor.rowcount == 1


def _encode(spec: AgentSpec) -> str:
    return json.dumps(
        {
            "id": spec.id,
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
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode(value: str) -> AgentSpec:
    payload = json.loads(value)
    return AgentSpec(
        id=payload["id"],
        name=payload["name"],
        name_zh=payload.get("name_zh", ""),
        name_en=payload.get("name_en", ""),
        description=payload.get("description", ""),
        description_zh=payload.get("description_zh", ""),
        soul=payload.get("soul", ""),
        model=parse_agent_model(payload.get("model")),
        tools=tuple(payload.get("tools", ())),
        skills=tuple(payload.get("skills", ())),
        mcps=tuple(payload.get("mcps", ())),
        plugins=tuple(payload["plugins"]) if "plugins" in payload else None,
        permissions=payload.get("permissions", {}),
        metadata=payload.get("metadata", {}),
    )


__all__ = ["SQLiteAgentStore"]
