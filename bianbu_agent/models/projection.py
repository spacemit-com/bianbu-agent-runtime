"""Public, credential-safe projections of stored Model records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from math import isfinite
from urllib.parse import urlsplit, urlunsplit

from bianbu_agent.models.file_store import ModelConfig


_PUBLIC_STRING_PARAMETERS = frozenset({
    "custom_id",
    "name",
    "record_kind",
})
_PUBLIC_NUMBER_PARAMETERS: frozenset[str] = frozenset()
_CREDENTIAL_SEGMENTS = frozenset(
    {
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
    }
)
_CREDENTIAL_PHRASES = frozenset(
    {
        ("access", "key"),
        ("access", "token"),
        ("api", "key"),
        ("api", "token"),
        ("auth", "token"),
        ("bearer", "token"),
        ("client", "key"),
        ("client", "token"),
        ("private", "key"),
        ("refresh", "token"),
        ("secret", "key"),
        ("session", "token"),
        ("signing", "key"),
    }
)
_CAMEL_SEGMENT = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+"
)


def model_config_view(config: ModelConfig) -> dict[str, object]:
    """Return the credential-free identity of one Model record."""

    return {
        "id": config.id,
        "provider": config.provider,
        "model": config.model,
        "base_url": model_base_url_view(config.base_url),
        "parameters": _public_parameters(config.parameters),
    }


def model_parameters_view(parameters: Mapping[str, object]) -> dict[str, object]:
    """Return all recursively JSON-safe Model parameters except credentials."""

    return {
        str(key): _public_parameter_value(value)
        for key, value in sorted(parameters.items(), key=lambda item: str(item[0]))
        if not is_credential_parameter(str(key))
    }


def _public_parameters(parameters: Mapping[str, object]) -> dict[str, object]:
    selected: dict[str, object] = {}
    for key in _PUBLIC_STRING_PARAMETERS:
        value = parameters.get(key)
        if isinstance(value, str):
            selected[key] = value
    for key in _PUBLIC_NUMBER_PARAMETERS:
        value = parameters.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not isinstance(value, float) or isfinite(value):
                selected[key] = value
    return {key: selected[key] for key in sorted(selected)}


def _public_parameter_value(value: object) -> object:
    if isinstance(value, Mapping):
        return model_parameters_view(value)
    if isinstance(value, (list, tuple)):
        return [_public_parameter_value(item) for item in value]
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("Model parameter numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported Model parameter value: {type(value).__name__}")


def is_credential_parameter(name: str) -> bool:
    """Return whether a parameter name denotes credentials, not a substring."""

    segments = _parameter_name_segments(name)
    if any(segment in _CREDENTIAL_SEGMENTS for segment in segments):
        return True
    if segments and segments[-1] in {"key", "token"}:
        return True
    return any(
        segments[index : index + 2] in _CREDENTIAL_PHRASES
        for index in range(max(0, len(segments) - 1))
    )


def _parameter_name_segments(name: str) -> tuple[str, ...]:
    return tuple(
        match.group(0).casefold()
        for part in re.split(r"[^A-Za-z0-9]+", str(name))
        for match in _CAMEL_SEGMENT.finditer(part)
    )


def model_base_url_view(value: str) -> str:
    """Return only the non-credential portion of an HTTP(S) model URL."""

    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    authority = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit((parsed.scheme.casefold(), authority, parsed.path, "", ""))


__all__ = [
    "is_credential_parameter",
    "model_base_url_view",
    "model_config_view",
    "model_parameters_view",
]
