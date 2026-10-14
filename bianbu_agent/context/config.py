from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from bianbu_agent.context.model_catalog import builtin_model_profile


DEFAULT_CONTEXT_WINDOW_TOKENS = 8_192
_CONTEXT_KEYS = (
    "max_input_tokens",
    "context_window",
    "context_length",
    "max_context_tokens",
)
_OUTPUT_KEYS = (
    "max_output_tokens",
    "output_token_limit",
    "max_completion_tokens",
)


@dataclass(frozen=True, slots=True)
class CompressionSettings:
    context_window_tokens: int
    output_reserve: int
    safety_reserve: int
    usable_input_tokens: int
    threshold_tokens: int
    target_tokens: int
    protect_first_n: int
    protect_last_n: int
    abort_on_summary_failure: bool
    window_source: str

    def with_limits(self, **changes: object) -> CompressionSettings:
        return replace(self, **changes)


class CompressionSettingsProvider:
    """Resolve dynamic runtime settings without rebuilding cached engines."""

    def read(self, model: object | None) -> CompressionSettings:
        sources = _model_sources(model)
        total = DEFAULT_CONTEXT_WINDOW_TOKENS
        source = "fallback_conservative"
        for candidate_source, metadata in sources:
            selected = _first_integer(metadata, _CONTEXT_KEYS)
            if selected is not None and selected >= 1_000:
                total = selected
                source = str(metadata.get("context_source") or candidate_source)
                break
        reserve = _output_reserve(sources, model, total)
        safety = min(max(128, math.ceil(total * .05)), max(1, total // 4))
        reserve = min(reserve, total - safety - 1)
        usable = total - reserve - safety
        return CompressionSettings(
            context_window_tokens=total,
            output_reserve=reserve,
            safety_reserve=safety,
            usable_input_tokens=usable,
            threshold_tokens=usable,
            target_tokens=max(1, int(usable * .50)),
            protect_first_n=0,
            protect_last_n=12,
            abort_on_summary_failure=True,
            window_source=source,
        )


def bounded_output_settings(
    model: object, values: Mapping[str, Any], *, default_key: str = "max_tokens",
) -> dict[str, Any]:
    """Bound invocation/wire options without mutating model or caller settings."""
    result = dict(values)
    settings = CompressionSettingsProvider().read(model)
    if settings.window_source == "fallback_conservative":
        return result
    cap = settings.output_reserve
    extra = result.get("extra_body", getattr(model, "extra_body", None))
    has_extra = isinstance(extra, Mapping)
    extra = dict(extra) if isinstance(extra, Mapping) else {}
    names = ("max_tokens", "max_completion_tokens", "max_output_tokens")
    for options in (result, extra):
        for name in names:
            requested = _positive_integer(options.get(name))
            if requested is not None:
                cap = min(cap, requested)
    key = next((name for name in names if name in result), default_key)
    for name in names:
        result.pop(name, None)
        extra.pop(name, None)
    if has_extra or "extra_body" in result:
        result["extra_body"] = extra
    result[key] = cap
    return result


def _model_sources(model: object | None) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    sources: list[tuple[str, Mapping[str, Any]]] = []
    for source, name in (("metadata", "metadata"), ("profile", "profile")):
        try:
            value = getattr(model, name, None)
        except Exception:
            value = None
        if isinstance(value, Mapping):
            if source == "metadata":
                for nested_name in ("model_profile", "profile"):
                    nested = value.get(nested_name)
                    if isinstance(nested, Mapping):
                        sources.append((f"metadata.{nested_name}", nested))
            sources.append((source, value))
    # A generation may retain its model object after the discovery cache
    # expires. Do not fall through to the same copied LangChain profile when
    # its observed serving limit is stale.
    if any(isinstance(metadata.get("context_expires_at"), (int, float))
           and metadata["context_expires_at"] <= time.time()
           for _source, metadata in sources):
        sources = []
    name = getattr(model, "model_name", None) or getattr(model, "model", None)
    builtin = builtin_model_profile(name)
    if builtin:
        sources.append(("builtin.model_catalog", builtin))
    return tuple(sources)


def _output_reserve(
    sources: tuple[tuple[str, Mapping[str, Any]], ...],
    model: object | None,
    total: int,
) -> int:
    # One budget governs both the wire request and compaction headroom.
    # A smaller explicit request or serving limit can only lower the 25% cap.
    reserve = max(1, total // 4)
    for name in ("max_tokens", "max_completion_tokens"):
        selected = _positive_integer(getattr(model, name, None))
        if selected is not None:
            reserve = min(reserve, selected)
    for _source, metadata in sources:
        selected = _first_integer(metadata, _OUTPUT_KEYS)
        if selected is not None:
            reserve = min(reserve, selected)
    return reserve


def _first_integer(values: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        selected = _positive_integer(values.get(key))
        if selected is not None:
            return selected
    return None


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        return None
    try:
        selected = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return selected if selected > 0 else None



__all__ = [
    "CompressionSettings",
    "CompressionSettingsProvider",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
]
