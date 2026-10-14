"""Read-only local model status owned by the AI Gateway.

The two fixed local models (Bibit intent LLM and embedding) belong to the
``spacemit-ai-gateway`` package.  ``bibit-intent`` only fronts that Gateway and
publishes the browser-facing report on ``GET /v1/models``.  The Runtime reports
what those packages publish so the Console and the Bibit settings plugin can
show the state read-only; download and load/unload stay with the Gateway, so
this resource never exposes lifecycle actions.

The two models are registered in the Gateway under the ids ``bibit-llm`` and
``bibit-embed``.  Those ids say which service call them, not which model is on
the device, so every report also carries the concrete artifact file name taken
from the Gateway's registered model path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import PurePosixPath

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from bianbu_agent.api.errors import success


#: Bibit Intent fronts the Gateway and publishes the two fixed local models.
LOCAL_MODEL_BASE_URL_ENV = "BIBIT_INTENT_BASE_URL"
LOCAL_MODEL_DEFAULT_BASE_URL = "http://127.0.0.1:8765"
#: The Gateway itself owns model downloads and loading; used when Intent is absent.
AI_GATEWAY_BASE_URL_ENV = "AI_GATEWAY_BASE_URL"
AI_GATEWAY_DEFAULT_BASE_URL = "http://127.0.0.1:18790"

_LOCAL_MODEL_TIMEOUT_SECONDS = 10.0
_MODEL_TYPES = ("llm", "embed")
#: Gateway statuses that mean the model artifact is present on this device.
_GATEWAY_LOCAL_STATUSES = frozenset({"downloaded", "loading", "loaded"})


def _clean_base_url(value: str, label: str) -> str:
    cleaned = str(value).strip().rstrip("/")
    if not cleaned:
        raise ValueError(f"{label} URL must not be empty")
    return cleaned


def _artifact_name(value: object) -> str | None:
    """Return the concrete model file name behind a registered model path."""

    if not isinstance(value, str):
        return None
    return PurePosixPath(value.strip()).name or None


def _progress(value: object) -> int:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, int(number)))


class LocalModelClient:
    """Report local model state without owning any model lifecycle."""

    def __init__(self, intent_base_url: str, gateway_base_url: str) -> None:
        self._intent_base_url = _clean_base_url(intent_base_url, "Bibit Intent")
        self._gateway_base_url = _clean_base_url(gateway_base_url, "AI Gateway")

    async def describe(self) -> Mapping[str, object]:
        failures: list[str] = []
        try:
            report = self._intent_report(
                await self._get(self._intent_base_url, "/v1/models", label="Bibit Intent")
            )
        except (RuntimeError, ValueError) as exc:
            failures.append(str(exc))
        else:
            return await self._with_artifacts(report)
        try:
            return await self._gateway_report()
        except (RuntimeError, ValueError) as exc:
            failures.append(str(exc))
        raise RuntimeError("; ".join(failures))

    async def _with_artifacts(self, report: Mapping[str, object]) -> Mapping[str, object]:
        """Name the concrete artifact behind a report that only has Gateway ids.

        Bibit Intent calls the two Gateway entries ``bibit-llm`` and
        ``bibit-embed``; only the Gateway registry knows which file backs each
        id.  Resolution is best-effort: a Gateway outage leaves the report as
        Intent published it.
        """

        models = report.get("models")
        if not isinstance(models, list):
            return report
        pending = [
            model
            for model in models
            if isinstance(model, dict) and not model.get("artifact")
        ]
        if not pending:
            return report
        artifacts = await self._gateway_artifacts()
        for model in pending:
            resolved = artifacts.get(str(model.get("name") or ""))
            if resolved is None:
                continue
            path, artifact = resolved
            model["path"] = model.get("path") or path
            model["artifact"] = artifact
        return report

    async def _gateway_artifacts(self) -> Mapping[str, tuple[str, str]]:
        """Map every Gateway model id to its registered path and file name."""

        artifacts: dict[str, tuple[str, str]] = {}
        for model_type in _MODEL_TYPES:
            try:
                rows = await self._get(
                    self._gateway_base_url,
                    f"/v1/{model_type}/models",
                    label="AI Gateway",
                )
            except RuntimeError:
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                model_id = str(row.get("id") or "").strip()
                path = row.get("local_path") or row.get("url")
                artifact = _artifact_name(path)
                if model_id and artifact and isinstance(path, str):
                    artifacts[model_id] = (path.strip(), artifact)
        return artifacts

    def _intent_report(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("Bibit Intent service returned no model list")
        models = [
            view
            for view in (_intent_model_view(model) for model in raw_models)
            if view is not None
        ]
        if not models:
            raise ValueError("Bibit Intent service returned no local models")
        return {"source": "bibit-intent", "read_only": True, "models": models}

    async def _gateway_report(self) -> Mapping[str, object]:
        models: list[Mapping[str, object]] = []
        for model_type in _MODEL_TYPES:
            rows = await self._get(
                self._gateway_base_url,
                f"/v1/{model_type}/models",
                label="AI Gateway",
            )
            if not isinstance(rows, list):
                raise RuntimeError("AI Gateway returned no model list")
            for row in rows:
                view = _gateway_model_view(model_type, row)
                if view is not None:
                    models.append(view)
        if not models:
            raise RuntimeError("AI Gateway reports no local models")
        return {"source": "ai-gateway", "read_only": True, "models": models}

    async def _get(self, base_url: str, path: str, *, label: str) -> object:
        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                timeout=httpx.Timeout(_LOCAL_MODEL_TIMEOUT_SECONDS),
            ) as client:
                response = await client.get(path)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"{label} request failed ({base_url}{path}): {exc}") from None


def _intent_model_view(model: object) -> Mapping[str, object] | None:
    """Project one Intent model report onto the read-only contract."""

    if not isinstance(model, Mapping):
        return None
    model_type = str(model.get("model_type") or "").strip()
    name = str(model.get("name") or "").strip()
    if model_type not in _MODEL_TYPES or not name:
        return None
    download = model.get("download")
    server = model.get("server")
    download = download if isinstance(download, Mapping) else {}
    server = server if isinstance(server, Mapping) else {}
    path = model.get("path")
    path = str(path) if isinstance(path, str) and path else None
    return {
        "model_type": model_type,
        "name": name,
        "artifact": _artifact_name(path),
        "backend": str(model.get("backend") or "ai_gateway"),
        "port": model.get("port"),
        "path": path,
        "registered": bool(model.get("registered", True)),
        "download": {
            "status": str(download.get("status") or "unknown"),
            "progress": _progress(download.get("progress")),
        },
        "server": {
            "status": str(server.get("status") or "unknown"),
            "managed": bool(server.get("managed", True)),
        },
    }


def _gateway_model_view(model_type: str, row: object) -> Mapping[str, object] | None:
    """Project one Gateway registry row onto the read-only contract."""

    if not isinstance(row, Mapping):
        return None
    if row.get("source_type") not in {"local_path", "local_url"} or row.get("api_base_url"):
        return None
    status = str(row.get("status") or "")
    if status not in _GATEWAY_LOCAL_STATUSES:
        return None
    name = str(row.get("id") or "").strip()
    if not name:
        return None
    path = row.get("local_path") or row.get("url")
    path = str(path) if isinstance(path, str) and path else None
    server_status = "running" if status == "loaded" else "starting" if status == "loading" else "stopped"
    return {
        "model_type": model_type,
        "name": name,
        "artifact": _artifact_name(path),
        "backend": "ai_gateway",
        "port": None,
        "path": path,
        "registered": True,
        "download": {"status": "completed", "progress": 100},
        "server": {"status": server_status, "managed": True},
    }


def local_model_client() -> LocalModelClient:
    return LocalModelClient(
        os.environ.get(LOCAL_MODEL_BASE_URL_ENV, LOCAL_MODEL_DEFAULT_BASE_URL),
        os.environ.get(AI_GATEWAY_BASE_URL_ENV, AI_GATEWAY_DEFAULT_BASE_URL),
    )


router = APIRouter()


@router.get("/local-models")
async def describe_local_models() -> JSONResponse:
    """Report the Gateway-owned local models read-only."""

    try:
        return success(dict(await local_model_client().describe()))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            502, {"code": "bad_gateway", "message": str(exc)}
        ) from exc


__all__ = [
    "AI_GATEWAY_BASE_URL_ENV",
    "AI_GATEWAY_DEFAULT_BASE_URL",
    "LOCAL_MODEL_BASE_URL_ENV",
    "LOCAL_MODEL_DEFAULT_BASE_URL",
    "LocalModelClient",
    "local_model_client",
    "router",
]
