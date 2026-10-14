from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


class BianbuAgentError(Exception):
    """Base class for stable public Bianbu Agent failures."""


class RuntimeConfigError(BianbuAgentError, ValueError):
    """Raised when Runtime configuration is invalid."""


class ModelResolutionError(BianbuAgentError, LookupError):
    """Raised when a logical model ID cannot be resolved."""


class ModelRecordIntegrityError(ModelResolutionError):
    """Raised when a Model record mutation would break required references."""


class ModelRecordRollbackError(BianbuAgentError):
    """Raised when a Model record batch cannot fully restore old records."""

    def __init__(self, record_ids: tuple[str, ...]) -> None:
        self.record_ids = record_ids
        super().__init__(
            "Model record rollback failed: " + ", ".join(record_ids)
        )


class RuntimeStartupError(BianbuAgentError, RuntimeError):
    """Raised when Runtime composition cannot start safely."""


class AgentValidationError(BianbuAgentError, ValueError):
    """Raised when an Agent references an unavailable model or capability."""


@dataclass(frozen=True, slots=True)
class AgentReference:
    """One Agent that still selects a resource being removed.

    Used by both Provider deletion and the capability (Plugin/MCP/Tool/Skill)
    deletion checks, so a refusal always names an Expert a user can find.
    """

    id: str
    name: str = ""
    name_zh: str = ""
    name_en: str = ""

    @property
    def label(self) -> str:
        """Return the identity a user can recognise in the Expert list.

        The runtime ID is a content hash (``com.bianbu.278200e1617c``); a
        refusal that only quotes it is unactionable.  Keep the ID beside the
        display name so the message stays unambiguous when two Experts share
        a name.
        """

        display = self.name_zh or self.name
        return f"{display}（{self.id}）" if display else self.id


class ProviderInUseError(BianbuAgentError):
    """Raised when deleting a provider would orphan Agent model references."""

    def __init__(self, provider_id: str, references: Sequence[AgentReference]) -> None:
        self.provider_id = provider_id
        self.references = tuple(references)
        self.agent_ids = tuple(reference.id for reference in self.references)
        super().__init__(
            f"服务商 {provider_id} 正在被专家使用："
            + "、".join(reference.label for reference in self.references)
            + "。请先在这些专家里换用其它服务商或删除这些专家，再删除该服务商。"
        )


_RESOURCE_LABELS = {"agent": "专家", "provider": "服务商"}


class ResourceNotFoundError(BianbuAgentError, LookupError):
    """Raised when a mutation names a resource that no longer exists.

    ``KeyError`` renders as a bare key, so a client that shows the message got
    ``custom-provider-definitely-missing`` and no hint about what happened or
    which kind of resource it was.  ``kind`` stays a machine token
    (``"provider"``) so clients can branch on it; the label is only for the
    message.
    """

    def __init__(self, kind: str, resource_id: str) -> None:
        self.kind = kind
        self.resource_id = resource_id
        super().__init__(
            f"找不到{_RESOURCE_LABELS.get(kind, kind)} {resource_id}，它可能已被删除。"
        )


class CapabilityNotFoundError(BianbuAgentError, LookupError):
    """Raised when a requested capability does not exist."""


class PluginResolutionError(BianbuAgentError, ValueError):
    """Raised when an enabled Plugin set cannot be resolved."""


__all__ = [
    "AgentValidationError",
    "BianbuAgentError",
    "CapabilityNotFoundError",
    "ModelRecordIntegrityError",
    "ModelRecordRollbackError",
    "ModelResolutionError",
    "ProviderInUseError",
    "AgentReference",
    "PluginResolutionError",
    "ResourceNotFoundError",
    "RuntimeConfigError",
    "RuntimeStartupError",
]
