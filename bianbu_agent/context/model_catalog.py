"""Offline fallback capacities; observed serving metadata always takes priority.

Reviewed 2026-09-11. Exact model aliases only: never infer unknown versions,
quantizations, or deployment capacities from a model-name prefix.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelCapacity:
    names: tuple[str, ...]
    context_tokens: int
    max_output_tokens: int | None
    source_url: str
    checked_on: str = '2026-09-11'


MODEL_CAPACITIES = (
    ModelCapacity(
        ('deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp', 'deepseek-v4-pro'),
        1_000_000, 384_000, 'https://api-docs.deepseek.com/quick_start/pricing/',
    ),
    ModelCapacity(
        ('MiniMax-M3',), 1_000_000, None,
        'https://platform.minimax.io/docs/api-reference/text-openai-api',
    ),
    ModelCapacity(
        ('MiniMax-M2', 'MiniMax-M2.1', 'MiniMax-M2.1-highspeed',
         'MiniMax-M2.5', 'MiniMax-M2.5-highspeed', 'MiniMax-M2.7', 'MiniMax-M2.7-highspeed'),
        204_800, None, 'https://platform.minimax.io/docs/api-reference/text-openai-api',
    ),
    ModelCapacity(
        ('kimi-k3',), 1_000_000, None, 'https://platform.kimi.ai/docs/models',
    ),
    ModelCapacity(
        ('kimi-k2.6', 'kimi-k2.7-code'), 256_000, None,
        'https://platform.kimi.ai/docs/models',
    ),
    ModelCapacity(
        ('glm-5',), 200_000, 131_072, 'https://docs.z.ai/guides/llm/glm-5',
    ),
)
_BY_NAME = {name.casefold(): entry for entry in MODEL_CAPACITIES for name in entry.names}


def builtin_model_profile(name: object) -> dict[str, object]:
    entry = _BY_NAME.get(name.strip().casefold()) if isinstance(name, str) else None
    if entry is None:
        return {}
    result: dict[str, object] = {
        'context_window': entry.context_tokens,
        'context_source': 'builtin.model_catalog',
        'context_source_url': entry.source_url,
        'context_checked_on': entry.checked_on,
    }
    if entry.max_output_tokens is not None:
        result['max_output_tokens'] = entry.max_output_tokens
    return result
