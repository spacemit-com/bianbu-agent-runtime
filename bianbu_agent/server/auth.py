"""HTTP management boundary for the Runtime control plane.

The public chat/run stream remains usable by the desktop UI, while Agent,
package, Plugin, MCP, model and execution-management resources require an
operator token in production.  The token is supplied through a protected
environment file or ``BIANBU_RUNTIME_ADMIN_TOKEN``; it is never stored in the
Runtime YAML or returned by an API.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from collections.abc import Mapping

from fastapi import APIRouter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


_PUBLIC_PREFIXES = (
    "/api/v1/sessions",
)
_MANAGEMENT_PREFIXES = (
    "/api/v1/agent-top",
    "/api/v1/agents",
    "/api/v1/capabilities",
    "/api/v1/executions",
    "/api/v1/local-models",
    "/api/v1/logs",
    "/api/v1/operational-logs",
    "/api/v1/mcp",
    "/api/v1/mcp-servers",
    "/api/v1/mcp-health-checks",
    "/api/v1/models",
    "/api/v1/packages",
    "/api/v1/plugins",
    "/api/v1/providers",
    "/api/v1/provider-capture",
    "/api/v1/settings",
    "/api/v1/workspace",
)
_AUTH_SESSION_PATH = "/api/v1/auth/session"
_SESSION_COOKIE = "bianbu_admin_session"
# Temporarily disabled for every deployment, including Debian production.
# Re-enabling is a deliberate product decision, not an environment override.
ADMIN_AUTH_ENABLED = False


def _production() -> bool:
    return os.environ.get("BIANBU_RUNTIME_MODE", "development").strip().lower() == "production"


def _admin_token() -> str:
    direct = os.environ.get("BIANBU_RUNTIME_ADMIN_TOKEN", "").strip()
    if direct:
        return direct
    path = os.environ.get("BIANBU_RUNTIME_ADMIN_TOKEN_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def requires_admin(request: Request) -> bool:
    """Return whether this HTTP operation is an operator-management call."""

    path = request.url.path
    if not path.startswith("/api/v1/"):
        return False
    # The session endpoint is the browser login/logout boundary.  It performs
    # its own token check and must remain reachable without the middleware
    # rejecting the initial login request.
    if path == _AUTH_SESSION_PATH:
        return False
    if path == "/api/v1/runtime/status":
        return False
    if any(path == prefix or path.startswith(prefix + "/") for prefix in _PUBLIC_PREFIXES):
        return False
    if any(path == prefix or path.startswith(prefix + "/") for prefix in _MANAGEMENT_PREFIXES):
        return True
    # Unknown versioned routes fail through the normal router, but if a new
    # management route is added without updating this list, mutating methods
    # remain protected by default.
    return request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}


def _presented_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.cookies.get(_SESSION_COOKIE, "").strip()


def _valid_token(request: Request, expected: str, *, body_token: str = "") -> bool:
    supplied = _presented_token(request) or str(body_token or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _auth_unconfigured() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "admin_auth_unconfigured",
                    "message": "生产 Runtime 未配置管理 API 凭据"}},
        status_code=503,
    )


def _auth_required() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "admin_auth_required",
                    "message": "管理 API 需要管理员认证"}},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _body_token(request: Request) -> str:
    """Read only the optional JSON token field; never log or echo it."""

    try:
        payload = await request.json()
    except Exception:  # malformed/empty body is handled as missing token
        return ""
    if not isinstance(payload, Mapping):
        return ""
    value = payload.get("token", "")
    return value.strip() if isinstance(value, str) else ""


auth_router = APIRouter(prefix="/api/v1")


@auth_router.post("/auth/session")
async def create_admin_session(request: Request) -> Response:
    """Exchange a bearer/JSON token for an HttpOnly browser session cookie."""

    if not ADMIN_AUTH_ENABLED:
        return JSONResponse({"data": {"authenticated": True, "required": False}})
    expected = _admin_token()
    if _production() and not expected:
        return _auth_unconfigured()
    # Development without a configured token intentionally has no login gate.
    if not expected:
        return JSONResponse({"data": {"authenticated": True, "required": False}})
    body_token = await _body_token(request)
    if not _valid_token(request, expected, body_token=body_token):
        return _auth_required()
    response = JSONResponse({"data": {"authenticated": True, "required": True}})
    response.set_cookie(
        _SESSION_COOKIE,
        body_token or _presented_token(request),
        max_age=8 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@auth_router.get("/auth/session")
async def get_admin_session(request: Request) -> Response:
    """Return authentication state without exposing the configured token."""

    if not ADMIN_AUTH_ENABLED:
        return JSONResponse({"data": {"authenticated": True, "required": False}})
    expected = _admin_token()
    if _production() and not expected:
        return _auth_unconfigured()
    required = bool(expected)
    if required and not _valid_token(request, expected):
        return _auth_required()
    return JSONResponse({"data": {"authenticated": True, "required": required}})


@auth_router.delete("/auth/session")
async def delete_admin_session() -> Response:
    response = JSONResponse({"data": {"authenticated": False}})
    response.delete_cookie(_SESSION_COOKIE, path="/")
    return response


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if (
            not ADMIN_AUTH_ENABLED
            or not _production()
            or not requires_admin(request)
        ):
            return await call_next(request)
        expected = _admin_token()
        if not expected:
            return JSONResponse(
                {"error": {"code": "admin_auth_unconfigured",
                            "message": "生产 Runtime 未配置管理 API 凭据"}},
                status_code=503,
            )
        supplied = _presented_token(request)
        if not supplied or not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                {"error": {"code": "admin_auth_required",
                            "message": "管理 API 需要管理员认证"}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


__all__ = [
    "ADMIN_AUTH_ENABLED",
    "AdminAuthMiddleware",
    "auth_router",
    "requires_admin",
]
