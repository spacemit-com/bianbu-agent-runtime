"""Settings and log resources."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import fields
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bianbu_agent.api.contracts import (
    SettingsUpdateRequest,
)
from bianbu_agent.api.errors import success
from bianbu_agent.api.models import list_providers
from bianbu_agent.capture.llm import (
    MAX_LIST_RECORDS,
    MAX_PREVIEW_INDEX_BYTES,
    MAX_RECORD_BYTES,
    MAX_RETAINED_RECORDS,
    MAX_STORAGE_BYTES,
    PREVIEW_BYTES,
    RETENTION_SECONDS,
)
from bianbu_agent.config import update_runtime_config
from bianbu_agent.errors import RuntimeConfigError
from bianbu_agent.server.runtime import BianbuAgentRuntime


logger = logging.getLogger(__name__)

router = APIRouter()

# The Logs page is a finite snapshot: bound it so a long-lived device does not
# push its whole operational history through one SSE response.  The Bibit
# Runtime plugin re-reads this endpoint every 3s, so the tail has to stay small.
_LOG_STREAM_LIMIT = 500
_SECRET_PARTS = ("key", "secret", "token", "password")
_RUNTIME_DEFAULTS: dict[str, object] = {
    "search_strategy": "searxng",
    "self_evolution_enabled": False,
    "self_evolution_idle_minutes": 10,
    "self_evolution_min_turns": 6,
}
_RUNTIME_KEYS = frozenset(_RUNTIME_DEFAULTS)
_RUNTIME_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "self_evolution_idle_minutes": (1, 24 * 60),
    "self_evolution_min_turns": (1, 1_000),
}
_RUNTIME_FLOAT_RANGES: dict[str, tuple[float, float]] = {
}
_RUNTIME_BOOLEAN_KEYS = {
    "self_evolution_enabled",
}
class ProviderCaptureTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider_id: str = Field(min_length=1, max_length=256, strict=True)

    @field_validator("provider_id")
    @classmethod
    def canonical_provider_id(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 for character in value
        ):
            raise ValueError("provider_id must be a canonical string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("provider_id must be valid UTF-8") from exc
        return value


def _runtime(request: Request) -> BianbuAgentRuntime:
    return cast(BianbuAgentRuntime, request.app.state.runtime)


def _data_root(runtime: BianbuAgentRuntime) -> Path:
    return Path(runtime.config.data_dir)


@router.get("/settings")
async def get_settings(request: Request):
    return success(_settings_view(_runtime(request)))


@router.patch("/settings")
async def update_settings(payload: SettingsUpdateRequest, request: Request):
    runtime = _runtime(request)
    try:
        if payload.deployment is not None:
            if runtime.config.config_path is None:
                raise RuntimeConfigError(
                    "Runtime configuration is not backed by a YAML file"
                )
            updates = payload.deployment.model_dump(exclude_unset=True)
            if not updates:
                raise ValueError("no deployment settings provided")
            runtime.config = update_runtime_config(runtime.config.config_path, updates)
        else:
            assert payload.runtime is not None
            updates = payload.runtime.model_dump(exclude_unset=True)
            _update_runtime_settings(_data_root(runtime) / "config.json", updates)
    except (RuntimeConfigError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return success(_settings_view(runtime))


def _settings_view(runtime: BianbuAgentRuntime) -> dict[str, object]:
    deployment = {
        field.name: _json_value(getattr(runtime.config, field.name))
        for field in fields(runtime.config)
    }
    saved = _validate_runtime_values(
        {
            key: value
            for key, value in _read_json_object(
                _data_root(runtime) / "config.json", strict=True
            ).items()
            if key in _RUNTIME_KEYS
        }
    )
    visible_runtime = dict(_RUNTIME_DEFAULTS)
    visible_runtime.update(saved)
    return {
        "deployment": _sanitize_settings(deployment),
        "runtime": visible_runtime,
    }


def _update_runtime_settings(path: Path, updates: dict[str, Any]) -> None:
    if not updates:
        raise ValueError("no runtime settings provided")
    applied = _validate_runtime_values(updates)
    stored = _validate_runtime_values({
        key: value for key, value in _read_json_object(path, strict=True).items()
        if key in _RUNTIME_KEYS
    })
    stored.update(applied)
    _write_json_object(path, stored)


def _validate_runtime_values(
    values: dict[str, Any],
) -> dict[str, object]:
    applied: dict[str, object] = {}
    for key, value in values.items():
        if key not in _RUNTIME_KEYS:
            raise ValueError(f"unknown runtime setting: {key}")
        if key in _RUNTIME_INTEGER_RANGES:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"invalid value for {key}: expected an integer")
            low, high = _RUNTIME_INTEGER_RANGES[key]
            if not low <= value <= high:
                raise ValueError(f"invalid value for {key}: expected {low}..{high}")
        elif key in _RUNTIME_FLOAT_RANGES:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"invalid value for {key}: expected a number")
            value = float(value)
            low, high = _RUNTIME_FLOAT_RANGES[key]
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"invalid value for {key}: expected {low:g}..{high:g}")
        elif key in _RUNTIME_BOOLEAN_KEYS:
            if not isinstance(value, bool):
                raise ValueError(f"invalid value for {key}: expected a boolean")
        elif key == "search_strategy" and value != "searxng":
            raise ValueError("invalid value for search_strategy: expected searxng")
        applied[key] = value
    return applied


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def format_log_record(record: Mapping[str, object]) -> str:
    """Render one operational record as the single line both log UIs read.

    The Console colours a line by its ``[LEVEL]`` marker and the Bibit Runtime
    plugin shows the text as-is, so the timestamp, level and logger have to be
    in the text itself instead of only in the JSON fields.
    """
    level = str(record.get("level") or "INFO").upper()
    timestamp = float(record.get("timestamp") or 0.0)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    millis = f".{int((timestamp % 1) * 1000):03d}" if timestamp else ""
    logger_name = str(record.get("logger") or "bianbu-agent")
    line = f"{stamp}{millis} [{level}] {logger_name}: {record.get('message') or ''}"
    context = " ".join(
        f"{key}={record[key]}"
        for key in (
            "agent_id",
            "session_id",
            "run_id",
            "plugin_generation",
            "event",
            "run_status",
            "error_code",
            "duration_ms",
        )
        if record.get(key) not in (None, "")
    )
    return f"{line} {context}".rstrip()


@router.get("/logs")
async def logs(request: Request) -> StreamingResponse:
    """Stream the Runtime's own operational log as a finite SSE snapshot.

    The Monitor → Logs page was empty because this endpoint globbed
    ``logs/*.log``: the Runtime writes no such file, only the correlated JSONL
    ``logs/operational/runtime.jsonl``.  Read the same store that backs the
    structured ``/operational-logs`` view, so the page shows the records that
    actually exist.
    """
    runtime = _runtime(request)

    async def events():
        records = await asyncio.to_thread(
            runtime.runtime_log_store.query, limit=_LOG_STREAM_LIMIT
        )
        for cursor, record in enumerate(records, start=1):
            event = json.dumps(
                {"type": "log", "content": format_log_record(record)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"id: {cursor}\ndata: {event}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/provider-capture")
async def provider_capture_collection(request: Request) -> JSONResponse:
    return _capture_response(await _capture_view(_runtime(request)))


@router.put("/provider-capture")
async def select_provider_capture(
    payload: ProviderCaptureTargetRequest,
    request: Request,
) -> JSONResponse:
    runtime = _runtime(request)
    provider = next(
        (
            item
            for item in await list_providers(runtime.model_configs)
            if item.get("id") == payload.provider_id
        ),
        None,
    )
    if provider is None:
        raise HTTPException(
            404,
            "Configured Provider was not found",
        )
    if provider.get("configured") is not True:
        raise HTTPException(
            422,
            "Provider is not configured",
        )
    await asyncio.to_thread(runtime.provider_capture_store.select, payload.provider_id)
    return _capture_response(await _capture_view(runtime))


@router.delete("/provider-capture/target")
async def stop_provider_capture(request: Request) -> JSONResponse:
    runtime = _runtime(request)
    await asyncio.to_thread(runtime.provider_capture_store.stop)
    return _capture_response(await _capture_view(runtime))


@router.delete("/provider-capture/records")
async def clear_provider_capture_records(request: Request) -> JSONResponse:
    runtime = _runtime(request)
    await asyncio.to_thread(runtime.provider_capture_store.clear)
    return _capture_response(await _capture_view(runtime))


@router.get("/provider-capture/{record_id}")
async def provider_capture_record(record_id: str, request: Request) -> JSONResponse:
    record = await asyncio.to_thread(
        _runtime(request).provider_capture_store.get,
        record_id,
    )
    if record is None:
        raise HTTPException(
            404,
            "Provider capture record was not found",
        )
    return _capture_response(record)


async def _capture_view(runtime: BianbuAgentRuntime) -> dict[str, object]:
    return await asyncio.to_thread(_capture_view_sync, runtime)


def _capture_view_sync(runtime: BianbuAgentRuntime) -> dict[str, object]:
    return {
        "provider_id": runtime.provider_capture_store.target(),
        "limits": {
            "max_list_records": MAX_LIST_RECORDS,
            "max_preview_index_bytes": MAX_PREVIEW_INDEX_BYTES,
            "max_record_bytes": MAX_RECORD_BYTES,
            "max_retained_records": MAX_RETAINED_RECORDS,
            "max_storage_bytes": MAX_STORAGE_BYTES,
            "preview_bytes": PREVIEW_BYTES,
            "retention_seconds": RETENTION_SECONDS,
        },
        "records": runtime.provider_capture_store.list(),
    }


def _capture_response(data: object) -> JSONResponse:
    response = success(data)
    response.headers["Cache-Control"] = "no-store"
    return response


def _read_json_object(path: Path, *, strict: bool) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        if strict:
            raise ValueError("invalid runtime settings JSON") from None
        return {}
    if not isinstance(value, dict):
        if strict:
            raise ValueError("runtime settings root must be an object")
        return {}
    return value


def _write_json_object(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sanitize_settings(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_settings(item)
            for key, item in value.items()
            if not any(
                part in str(key).casefold().replace("-", "_")
                for part in _SECRET_PARTS
            )
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_settings(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_value(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


__all__ = ["router"]
