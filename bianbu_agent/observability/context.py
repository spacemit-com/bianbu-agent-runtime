"""Async-safe correlation scope for Runtime-owned logs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


_FIELDS = frozenset({
    "agent_id",
    "session_id",
    "run_id",
    "plugin_generation",
    "provider_request_id",
})
_CONTEXT: ContextVar[dict[str, object]] = ContextVar(
    "bianbu_runtime_log_context", default={}
)


def current_log_context() -> dict[str, object]:
    return dict(_CONTEXT.get())


@contextmanager
def bind_log_context(**fields: object) -> Iterator[None]:
    unknown = set(fields) - _FIELDS
    if unknown:
        raise ValueError(f"unsupported log context fields: {', '.join(sorted(unknown))}")
    merged = current_log_context()
    for key, value in fields.items():
        if value is None or value == "":
            merged.pop(key, None)
        elif key == "plugin_generation":
            merged[key] = int(value)
        else:
            merged[key] = str(value)
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


__all__ = ["bind_log_context", "current_log_context"]

