"""Run-local observation for Hook execution without coupling Plugins to Runs."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token


logger = logging.getLogger(__name__)
HookObserver = Callable[[dict[str, object]], Awaitable[None]]
_OBSERVER: ContextVar[HookObserver | None] = ContextVar(
    "bianbu_hook_observer",
    default=None,
)
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|api[_-]?key)",
    re.IGNORECASE,
)


def sanitize_observation(value: object, *, key: str = "") -> object:
    """Return a JSON-safe copy with credential-shaped fields removed."""

    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_observation(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_observation(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@contextmanager
def observe_hooks(observer: HookObserver):
    """Install one async observer for Hook calls in the current Run context."""

    token: Token[HookObserver | None] = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


async def emit_hook_observation(event: Mapping[str, object]) -> None:
    """Publish one sanitized fact; observation can never change Run behavior."""

    observer = _OBSERVER.get()
    if observer is None:
        return
    try:
        await observer(dict(sanitize_observation(event)))
    except Exception:  # noqa: BLE001
        logger.exception("Hook observation callback failed")


__all__ = ["emit_hook_observation", "observe_hooks", "sanitize_observation"]
