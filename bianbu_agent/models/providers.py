"""Provider credential values shared by Agent-owned model selections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from types import MappingProxyType
from urllib.parse import urlsplit


#: How long one model request may stay silent before the client gives up.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0

#: A model the device serves itself is bounded by its own compute, not by the
#: network. A 4B vision model on K3 spends minutes in image prefill, and a
#: non-streaming endpoint writes nothing at all until it is done -- the network
#: default cut such an answer off in the middle. Same reasoning as the loopback
#: default of the Ollama and LM Studio templates: a local socket is not slow
#: because the network is, so silence has to be judged more generously.
LOCAL_REQUEST_TIMEOUT_SECONDS = 900.0

#: Upper bound for the operator-provided override: one hour.
MAX_REQUEST_TIMEOUT_SECONDS = 3600.0

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


PROVIDER_TEMPLATES = (
    {
        "id": "openai",
        "name": "OpenAI",
        "protocol": "openai-compatible",
        "default_base_url": "https://api.openai.com/v1",
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "protocol": "openai-compatible",
        "default_base_url": "https://api.anthropic.com/v1",
    },
    {
        "id": "gemini",
        "name": "Gemini",
        "protocol": "openai-compatible",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "protocol": "openai-compatible",
        "default_base_url": "https://openrouter.ai/api/v1",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "openai-compatible",
        "default_base_url": "https://api.deepseek.com/v1",
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "protocol": "openai-compatible",
        "default_base_url": "https://api.minimaxi.com/v1",
    },
    {
        "id": "siliconflow",
        "name": "SiliconFlow",
        "protocol": "openai-compatible",
        "default_base_url": "https://api.siliconflow.cn/v1",
    },
    {
        "id": "mistral",
        "name": "Mistral",
        "protocol": "openai-compatible",
        "default_base_url": "https://api.mistral.ai/v1",
    },
    {
        "id": "groq",
        "name": "Groq",
        "protocol": "openai-compatible",
        "default_base_url": "https://api.groq.com/openai/v1",
    },
    {
        "id": "xai",
        "name": "xAI",
        "protocol": "openai-compatible",
        "default_base_url": "https://api.x.ai/v1",
    },
    {
        "id": "nvidia",
        "name": "NVIDIA",
        "protocol": "openai-compatible",
        "default_base_url": "https://integrate.api.nvidia.com/v1",
    },
    {
        "id": "huggingface",
        "name": "Hugging Face",
        "protocol": "openai-compatible",
        "default_base_url": "https://router.huggingface.co/v1",
    },
    {
        # Azure serves the OpenAI-compatible surface under a per-customer
        # resource name, so there is no default worth prefilling: leaving the
        # Base URL empty forces the user to type their own endpoint instead of
        # saving a placeholder that can never answer.
        "id": "azure",
        "name": "Azure OpenAI",
        "protocol": "openai-compatible",
        "default_base_url": "",
    },
    {
        "id": "ollama",
        "name": "Ollama",
        "protocol": "openai-compatible",
        "default_base_url": "http://127.0.0.1:11434/v1",
    },
    {
        "id": "lm-studio",
        "name": "LM Studio",
        "protocol": "openai-compatible",
        "default_base_url": "http://127.0.0.1:1234/v1",
    },
    {
        "id": "custom",
        "name": "Custom Provider",
        "protocol": "openai-compatible",
        "default_base_url": "",
    },
)


def normalize_provider_configuration(
    base_url: object,
    api_key: object,
) -> tuple[str, str]:
    """Return runtime-usable Provider connection strings without coercion."""

    normalized_base_url = base_url.strip() if isinstance(base_url, str) else ""
    normalized_api_key = api_key.strip() if isinstance(api_key, str) else ""
    return normalized_base_url, normalized_api_key


def resolve_request_timeout(
    base_url: object,
    parameters: Mapping[str, object] | None = None,
) -> float:
    """How long one request to this Provider may stay silent.

    ``parameters.request_timeout_seconds`` is the operator's explicit answer and
    wins. Otherwise the endpoint decides: a loopback Base URL gets the local
    floor because the wait is the device's own compute, and everything else
    keeps the network default. A malformed override is refused instead of being
    ignored, so a typo cannot quietly restore the timeout the operator was
    trying to raise.
    """

    supplied = (parameters or {}).get("request_timeout_seconds")
    if supplied is not None:
        if (
            isinstance(supplied, bool)
            or not isinstance(supplied, (int, float))
            or not math.isfinite(float(supplied))
        ):
            raise ValueError("request_timeout_seconds must be a finite number")
        seconds = float(supplied)
        if seconds <= 0 or seconds > MAX_REQUEST_TIMEOUT_SECONDS:
            raise ValueError(
                "request_timeout_seconds must be within "
                f"(0, {MAX_REQUEST_TIMEOUT_SECONDS:g}]"
            )
        return seconds
    host = (urlsplit(base_url).hostname or "").strip("[]").lower()
    return (
        LOCAL_REQUEST_TIMEOUT_SECONDS
        if host in _LOCAL_HOSTS
        else DEFAULT_REQUEST_TIMEOUT_SECONDS
    )


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Private configuration for one reusable model service provider."""

    id: str
    name: str
    protocol: str
    base_url: str
    api_key: str = field(default="", repr=False)
    parameters: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for field_name in ("id", "name", "protocol"):
            selected = str(getattr(self, field_name)).strip()
            if not selected:
                raise ValueError(f"provider.{field_name} must not be empty")
            object.__setattr__(self, field_name, selected)
        if any(part in self.id for part in ("/", "\\", "..")):
            raise ValueError("provider.id contains unsafe characters")
        base_url, api_key = normalize_provider_configuration(
            self.base_url,
            self.api_key,
        )
        if not base_url:
            raise ValueError("provider.base_url must not be empty")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(self.parameters)),
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "LOCAL_REQUEST_TIMEOUT_SECONDS",
    "MAX_REQUEST_TIMEOUT_SECONDS",
    "PROVIDER_TEMPLATES",
    "ProviderConfig",
    "normalize_provider_configuration",
    "resolve_request_timeout",
]
