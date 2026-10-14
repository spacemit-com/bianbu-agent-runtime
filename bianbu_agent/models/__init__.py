"""Model runtime contracts and file-backed model configuration."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from bianbu_agent.agents import AgentModel

from bianbu_agent.errors import (
    ModelRecordIntegrityError,
    ModelRecordRollbackError,
    ModelResolutionError,
)


@runtime_checkable
class ChatModel(Protocol):
    """Minimal framework-neutral identity required by public runtime calls."""

    model: str


class ModelResolver(Protocol):
    def resolve(self, model_id: str) -> ChatModel | None: ...


@dataclass(frozen=True, slots=True)
class OpenAICompatibleModel:
    model: str
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    provider_id: str = ""
    request_timeout_seconds: float = 120.0
    profile: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile", MappingProxyType(dict(self.profile)))
        model = str(self.model).strip()
        if not model:
            raise ValueError("model must not be empty")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "base_url", str(self.base_url).strip())
        provider_id = str(self.provider_id).strip()
        if len(provider_id) > 256 or any(
            ord(character) < 32 for character in provider_id
        ):
            raise ValueError("provider_id must be a canonical string")
        object.__setattr__(self, "provider_id", provider_id)
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        timeout = self.request_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise ValueError("request_timeout_seconds must be positive and finite")
        object.__setattr__(self, "request_timeout_seconds", float(timeout))


class ModelRegistry:
    def __init__(
        self,
        *,
        default_model: ChatModel | None,
        models: Mapping[str, ChatModel] | None = None,
        resolver: ModelResolver | None = None,
    ) -> None:
        self._default = default_model
        normalized: dict[str, ChatModel] = {}
        for key, model in (models or {}).items():
            alias = str(key).strip()
            if not alias:
                raise ValueError("model alias must not be empty")
            normalized[alias] = model
        self._models = MappingProxyType(normalized)
        self._resolver = resolver

    @property
    def models(self) -> Mapping[str, ChatModel]:
        return self._models

    @property
    def has_default_model(self) -> bool:
        """Whether an Agent without a model falls back to the injected default."""

        return self._default is not None

    def resolve(self, selected: ChatModel | AgentModel | str | None) -> ChatModel:
        if selected is None or (isinstance(selected, str) and not selected.strip()):
            if self._default is None:
                raise ModelResolutionError("agent_model_required")
            return self._default
        if isinstance(selected, AgentModel):
            resolve_agent_model = getattr(self._resolver, "resolve_agent_model", None)
            if not callable(resolve_agent_model):
                raise ModelResolutionError(
                    f"Agent provider could not be resolved: {selected.provider}"
                )
            return resolve_agent_model(selected)
        if not isinstance(selected, str):
            return selected
        model_id = selected.strip()
        model = self._models.get(model_id)
        if model is not None:
            return model
        raise ModelResolutionError(f"model could not be resolved: {model_id}")


from bianbu_agent.models.file_store import YAMLModelStore, ModelConfig
from bianbu_agent.models.file_resolver import YAMLModelResolver
from bianbu_agent.models.projection import (
    is_credential_parameter,
    model_base_url_view,
    model_config_view,
    model_parameters_view,
)
from bianbu_agent.models.providers import ProviderConfig


__all__ = [
    "ChatModel",
    "ModelRegistry",
    "ModelResolver",
    "YAMLModelStore",
    "ModelConfig",
    "ModelRecordIntegrityError",
    "ModelRecordRollbackError",
    "YAMLModelResolver",
    "OpenAICompatibleModel",
    "ProviderConfig",
    "is_credential_parameter",
    "model_base_url_view",
    "model_config_view",
    "model_parameters_view",
]
