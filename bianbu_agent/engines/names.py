from __future__ import annotations

from hashlib import sha256
import re
from threading import Lock

_MAX_PROVIDER_NAME = 64
_LOCK = Lock()
_DECODED: dict[str, str] = {}


def encode_tool_name(qualified_id: str) -> str:
    """Make a readable, provider-safe function name and remember its source."""

    normalized = str(qualified_id).strip()
    readable = re.sub(r"[^A-Za-z0-9_-]+", "__", normalized).strip("_") or "tool"
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    encoded = readable
    if len(encoded) > _MAX_PROVIDER_NAME:
        encoded = encoded[: _MAX_PROVIDER_NAME - 9].rstrip("_") + "_" + digest[:8]
    with _LOCK:
        existing = _DECODED.get(encoded)
        if existing is not None and existing != normalized:
            encoded = encoded[: _MAX_PROVIDER_NAME - 9].rstrip("_") + "_" + digest[:8]
            existing = _DECODED.get(encoded)
            if existing is not None and existing != normalized:
                raise ValueError("Tool name encoding collision")
        _DECODED[encoded] = normalized
    return encoded


def decode_tool_name(model_name: str) -> str:
    """Map an encoded model-facing name back to its canonical SDK Tool ID."""

    normalized = str(model_name)
    with _LOCK:
        return _DECODED.get(normalized, normalized)


__all__ = ["decode_tool_name", "encode_tool_name"]
