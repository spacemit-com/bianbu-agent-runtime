"""Stable JSON output projection for CLI resources."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any


def emit(value: object) -> None:
    print(json.dumps(plain(value), ensure_ascii=False, sort_keys=True))


def plain(value: object) -> Any:
    if is_dataclass(value):
        return {
            field.name: plain(getattr(value, field.name))
            for field in fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return plain(value.value)
    return value


__all__ = ["emit", "plain"]
