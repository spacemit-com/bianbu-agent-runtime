from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from bianbu_agent.agents import AgentSpec, agent_model_data, parse_agent_model


class DirectoryAgentStore:
    """One inspectable Agent declaration per managed directory."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser().resolve()

    async def open(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        return None

    async def get(self, agent_id: str) -> AgentSpec | None:
        path = self._path(agent_id)
        return _decode(path.read_text(encoding="utf-8")) if path.is_file() else None

    async def list(self) -> list[AgentSpec]:
        values = []
        for path in sorted(self._root.glob("*/agent.yaml")):
            values.append(_decode(path.read_text(encoding="utf-8")))
        return values

    async def create(self, spec: AgentSpec) -> AgentSpec:
        path = self._path(spec.id)
        if path.exists():
            raise ValueError(f"agent already exists: {spec.id}")
        created_directory = False
        try:
            path.parent.mkdir(parents=True)
            created_directory = True
            self._write(path, spec)
        except BaseException:
            if created_directory:
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
            raise
        return spec

    async def update(self, spec: AgentSpec) -> AgentSpec:
        path = self._path(spec.id)
        if not path.is_file():
            raise KeyError(spec.id)
        self._write(path, spec)
        return spec

    async def delete(self, agent_id: str) -> bool:
        path = self._path(agent_id)
        if not path.is_file():
            return False
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return True

    def _path(self, agent_id: str) -> Path:
        normalized = str(agent_id).strip()
        if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
            raise ValueError("invalid Agent ID")
        return self._root / normalized / "agent.yaml"

    @staticmethod
    def _write(path: Path, spec: AgentSpec) -> None:
        payload = _yaml_safe_value(
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
            }
        )
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".agent-",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                yaml.safe_dump(payload, stream, allow_unicode=True, sort_keys=False)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _yaml_safe_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _yaml_safe_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_yaml_safe_value(item) for item in value]
    return value


def _decode(text: str) -> AgentSpec:
    payload = yaml.safe_load(text)
    if not isinstance(payload, Mapping):
        raise ValueError("agent.yaml root must be a mapping")
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
        plugins=tuple(payload.get("plugins", ())),
        permissions=payload.get("permissions", {}),
        metadata=payload.get("metadata", {}),
    )


__all__ = ["DirectoryAgentStore"]
