"""One JSON error envelope for the public and administration APIs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from bianbu_agent.errors import (
    ModelRecordIntegrityError,
    ModelRecordRollbackError,
    ProviderInUseError,
    ResourceNotFoundError,
)
from bianbu_agent.security.security_provider_policy import ProviderHostRejected
from bianbu_agent.sessions import SessionAgentMismatchError


def success(data: Any, *, status_code: int = 200) -> JSONResponse:
    """Wrap a successful JSON response in the common API envelope."""

    return JSONResponse({"data": jsonable_encoder(data)}, status_code=status_code)


def install_error_handlers(app: FastAPI) -> None:
    """Install deterministic API error serialization for framework and domain errors."""

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors = jsonable_encoder(exc.errors())
        if any(item.get("type") == "json_invalid" for item in errors):
            return _request_headers(
                request,
                _error_response(
                    400,
                    "bad_request",
                    "Malformed JSON request body.",
                    {"errors": errors},
                ),
            )
        return _request_headers(
            request,
            _error_response(
                422,
                "validation_error",
                "Request validation failed.",
                {"errors": errors},
            ),
        )

    @app.exception_handler(HTTPException)
    async def fastapi_http_error(
        request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        return _request_headers(
            request,
            _http_error_response(exc.status_code, exc.detail),
        )

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_error(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        return _request_headers(
            request,
            _http_error_response(exc.status_code, exc.detail),
        )

    @app.exception_handler(SessionAgentMismatchError)
    async def session_agent_mismatch(_: Request, exc: SessionAgentMismatchError) -> JSONResponse:
        return _error_response(
            409,
            "session_agent_mismatch",
            str(exc),
            {
                "session_id": exc.session_id,
                "expected_agent_id": exc.expected,
                "supplied_agent_id": exc.supplied,
            },
        )

    @app.exception_handler(ModelRecordIntegrityError)
    async def model_integrity_error(
        request: Request,
        exc: ModelRecordIntegrityError,
    ) -> JSONResponse:
        return _request_headers(
            request,
            _error_response(409, "model_integrity_conflict", str(exc)),
        )

    @app.exception_handler(ModelRecordRollbackError)
    async def model_rollback_error(
        request: Request,
        exc: ModelRecordRollbackError,
    ) -> JSONResponse:
        return _request_headers(
            request,
            _error_response(
                500,
                "model_rollback_failed",
                str(exc),
                {"record_ids": list(exc.record_ids)},
            ),
        )

    @app.exception_handler(ProviderInUseError)
    async def provider_in_use(_: Request, exc: ProviderInUseError) -> JSONResponse:
        return _error_response(
            409,
            "provider_in_use",
            str(exc),
            {
                "provider_id": exc.provider_id,
                "agent_ids": list(exc.agent_ids),
                "agents": [
                    {
                        "id": reference.id,
                        "name": reference.name,
                        "name_zh": reference.name_zh,
                        "name_en": reference.name_en,
                    }
                    for reference in exc.references
                ],
            },
        )

    @app.exception_handler(ResourceNotFoundError)
    async def resource_not_found(_: Request, exc: ResourceNotFoundError) -> JSONResponse:
        """404 for mutations: name the resource instead of echoing a bare key.

        The generic ``KeyError`` handler renders ``str(exc)`` with the quotes
        stripped, so a client that only shows the message saw
        ``custom-provider-definitely-missing`` -- no kind, no reason, and not
        even the ID the user clicked.
        """

        return _error_response(
            404,
            "not_found",
            str(exc),
            {"kind": exc.kind, "id": exc.resource_id},
        )

    @app.exception_handler(ProviderHostRejected)
    async def provider_host_rejected(_: Request, exc: ProviderHostRejected) -> JSONResponse:
        """D-8：base_url 不在白名单。

        必须有专门的处理器：否则落到下面的 Exception 兜底，变成 500 且原因
        被替换成 "Internal server error." —— 用户只看到"失败了"，不知道是
        自己配的 host 不在允许列表内，也不知道该改哪个环境变量。
        403 而非 422：这不是格式错误，是策略拒绝。
        """
        return _error_response(
            403,
            "provider_host_rejected",
            str(exc),
            {"host": exc.host, "allowed_hosts": list(exc.allowed)},
        )

    @app.exception_handler(KeyError)
    async def missing_resource(request: Request, exc: KeyError) -> JSONResponse:
        return _request_headers(
            request,
            _error_response(404, "not_found", _message(exc)),
        )

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        return _request_headers(
            request,
            _error_response(422, "validation_error", _message(exc)),
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, _: Exception):
        if _is_api_path(request.url.path):
            return _request_headers(
                request,
                _error_response(
                    500,
                    "internal_error",
                    "Internal server error.",
                ),
            )
        return PlainTextResponse("Internal Server Error", status_code=500)


def _http_error_response(status_code: int, detail: object) -> JSONResponse:
    if isinstance(detail, Mapping):
        code = str(detail.get("code") or _status_code(status_code))
        message = str(detail.get("message") or code.replace("_", " "))
        details = detail.get("details")
        return _error_response(
            status_code,
            code,
            message,
            dict(details) if isinstance(details, Mapping) else {},
        )
    return _error_response(status_code, _status_code(status_code), str(detail))


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "details": jsonable_encoder(dict(details or {})),
            }
        },
        status_code=status_code,
    )


def _message(exc: Exception) -> str:
    return str(exc).strip("'\"") or exc.__class__.__name__


def _request_headers(request: Request, response: JSONResponse) -> JSONResponse:
    path = request.url.path
    if path == "/api/v1/provider-capture" or path.startswith(
        "/api/v1/provider-capture/"
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


def _status_code(status_code: int) -> str:
    return {
        400: "bad_request",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
        422: "validation_error",
        500: "internal_error",
        502: "bad_gateway",
    }.get(status_code, "http_error")


def _is_api_path(path: str) -> bool:
    return path == "/api/v1" or path.startswith("/api/v1/")


__all__ = ["install_error_handlers", "success"]
