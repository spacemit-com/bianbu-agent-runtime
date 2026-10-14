"""Observed serving limits, cached with provider identity rather than user knobs."""
from collections.abc import Mapping, Sequence
import time

from bianbu_agent.context.config import _positive_integer

CACHE_MAX_AGE_SECONDS = 86_400


def is_llama_server(data: Sequence[object]) -> bool:
    return any(isinstance(item, Mapping) and (
        str(item.get("owned_by", "")).lower() in {"llamacpp", "llama.cpp", "llama-box"}
        or isinstance(item.get("meta"), Mapping) and "n_ctx_train" in item["meta"]
    ) for item in data)


def discovered_profiles(data: Sequence[object], props: object = None) -> dict[str, dict[str, object]]:
    serving = None
    if isinstance(props, Mapping):
        defaults = props.get("default_generation_settings")
        if isinstance(defaults, Mapping):
            serving = _positive_integer(defaults.get("n_ctx"))
    profiles = {}
    for item in data:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        profile = {}
        # n_ctx_train is a training capacity, never a deployed request limit.
        for key in ("context_length", "context_window", "max_input_tokens", "max_model_len", "n_ctx"):
            value = _positive_integer(item.get(key))
            if value is not None:
                profile = {"max_input_tokens": value, "context_source": "provider.models"}
                break
        if serving is not None and len(data) == 1:
            profile = {"max_input_tokens": serving, "context_source": "provider.props"}
        if not profile:
            continue
        top = item.get("top_provider")
        for values in (item, top if isinstance(top, Mapping) else {}):
            output = _positive_integer(values.get("max_output_tokens") or values.get("max_completion_tokens"))
            if output is not None:
                profile["max_output_tokens"] = output
                break
        profiles[str(item["id"])] = profile
    return profiles


def cached_profile(cache: object, base_url: str, model: str, *, now: float | None = None) -> dict[str, object]:
    if not isinstance(cache, Mapping) or cache.get("base_url") != base_url.rstrip("/"):
        return {}
    observed = cache.get("observed_at")
    if not isinstance(observed, (int, float)):
        return {}
    age = (time.time() if now is None else now) - observed
    if not 0 <= age <= CACHE_MAX_AGE_SECONDS:
        return {}
    models = cache.get("models")
    profile = models.get(model) if isinstance(models, Mapping) else None
    return {**profile, "context_expires_at": observed + CACHE_MAX_AGE_SECONDS} if isinstance(profile, Mapping) else {}
