"""Resolve file-backed model configuration into runtime chat models."""

from __future__ import annotations

from pathlib import Path

from bianbu_agent.agents import AgentModel

from bianbu_agent.errors import CapabilityNotFoundError, ModelResolutionError
from bianbu_agent.models.file_store import (
    ModelConfig,
    _LayeredModelRecordDirectory,
    _LayeredModelPairJournal,
    _decode,
    _provider_record_id,
    _validate_transaction_snapshot,
)
from bianbu_agent.models._record_lock import ModelRecordFileLock
from bianbu_agent.models import OpenAICompatibleModel
from bianbu_agent.models.context_profiles import cached_profile
from bianbu_agent.models.providers import (
    normalize_provider_configuration,
    resolve_request_timeout,
)


def _durability_boundary(name: str) -> None:
    """Internal recovery crash-injection seam."""


class YAMLModelResolver:
    """Synchronously resolve atomically-written provider/model YAML records."""

    def __init__(self, root: Path) -> None:
        self._records = _LayeredModelRecordDirectory(root)
        self._transaction_lock = ModelRecordFileLock(self._records.root)
        self._transaction_journal = _LayeredModelPairJournal(
            self._records.root, user_records=self._records.user,
            boundary=lambda name: _durability_boundary(name),
            validate_snapshot=_validate_transaction_snapshot,
        )

    def resolve_agent_model(self, selected: AgentModel) -> OpenAICompatibleModel:
        """Resolve exactly the provider/model pair stored by one Agent."""

        if not isinstance(selected, AgentModel):
            raise TypeError("selected must be an AgentModel")
        with self._transaction_lock.exclusive():
            self._transaction_journal.recover(self._records)
            credential_id = _provider_record_id(selected.provider)
            payload = self._records.read(credential_id)
            if payload is None:
                raise ModelResolutionError(
                    f"Provider credential record missing: {credential_id}"
                )
            credential = _decode(payload)
            protocol = str(
                credential.parameters.get("protocol") or "openai-compatible"
            ).strip()
            if protocol != "openai-compatible":
                raise ModelResolutionError(
                    f"unsupported Provider protocol: {protocol}"
                )
            base_url, api_key = normalize_provider_configuration(
                credential.base_url,
                credential.parameters.get("api_key"),
            )
            if not api_key:
                raise ModelResolutionError(
                    f"Provider API key missing: {credential_id}"
                )
            if not base_url:
                raise ModelResolutionError(
                    f"Provider Base URL missing: {credential_id}"
                )
            return OpenAICompatibleModel(
                model=selected.name,
                api_key=api_key,
                base_url=base_url,
                provider_id=selected.provider,
                request_timeout_seconds=resolve_request_timeout(
                    base_url, credential.parameters
                ),
                profile=cached_profile(credential.parameters.get("observed_model_profiles"),
                                       base_url, selected.name),
            )

    def resolve_capability(
        self, name: str, *, required: bool = False
    ) -> OpenAICompatibleModel | None:
        capability = str(name).strip().lower()
        if not capability:
            raise ValueError("capability name must not be empty")
        record_id = f"capability-{capability}"
        with self._transaction_lock.exclusive():
            self._transaction_journal.recover(self._records)
            payload = self._records.read(record_id)
            if payload is None:
                if required:
                    raise CapabilityNotFoundError(
                        f"required capability record missing: {record_id}"
                    )
                return None
            return self._resolve_target(_decode(payload))

    def _resolve_target(self, target: ModelConfig) -> OpenAICompatibleModel:
        credential_id = _provider_record_id(target.provider)
        payload = self._records.read(credential_id)
        if payload is None:
            raise ModelResolutionError(
                "Provider credential record missing: "
                f"{credential_id}, {target.id}"
            )
        credential = _decode(payload)
        base_url, api_key = normalize_provider_configuration(
            credential.base_url,
            credential.parameters.get("api_key"),
        )
        if not api_key:
            raise ModelResolutionError(
                f"Provider API key missing: {credential_id}"
            )
        if not base_url:
            raise ModelResolutionError(
                f"Provider Base URL missing: {credential_id}"
            )
        return OpenAICompatibleModel(
            model=target.model,
            api_key=api_key,
            base_url=base_url,
            provider_id=target.provider,
            request_timeout_seconds=resolve_request_timeout(
                base_url, credential.parameters
            ),
            profile=cached_profile(credential.parameters.get("observed_model_profiles"),
                                   base_url, target.model),
        )


__all__ = ["YAMLModelResolver"]
