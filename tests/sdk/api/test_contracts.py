from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from bianbu_agent.api import contracts
from bianbu_agent.api.contracts import (
    AgentCreateRequest,
    AgentUpdateRequest,
    RuntimeSettingsRequest,
    SessionPromptRequest,
)
from bianbu_agent.api.errors import install_error_handlers
from bianbu_agent.server.app import create_app


def test_session_prompt_request_is_strict_and_steer_only() -> None:
    accepted = contracts.SessionPromptRequest.model_validate(
        {
            "message_id": "browser-message-0001",
            "content": "focus on the failing test",
            "attachments": [{"name": "results.txt", "size": 42}],
        }
    )
    assert accepted.attachments == [{"name": "results.txt", "size": 42}]

    for payload in (
        {"message_id": "message-1", "content": ""},
        {"message_id": "contains a space", "content": "next"},
        {"message_id": "message-1", "mode": "steer", "content": "next"},
        {"message_id": "message-1", "content": "next", "extra": True},
    ):
        with pytest.raises(ValueError):
            contracts.SessionPromptRequest.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    (
        {"name": "Missing"},
        {"name": "Null", "model": None},
    ),
)
def test_agent_create_allows_model_to_be_configured_later(payload: dict[str, object]) -> None:
    """An expert may be saved before the user chooses its model."""

    request = AgentCreateRequest.model_validate(payload)
    assert request.model is None


def test_agent_create_rejects_caller_supplied_id() -> None:
    """Runtime owns logical Agent IDs instead of accepting caller identities."""

    with pytest.raises(ValueError):
        AgentCreateRequest.model_validate(
            {
                "id": "caller-selected",
                "name": "Caller Selected",
                "model": {"provider": "provider", "name": "model"},
            }
        )


def test_agent_patch_distinguishes_clearing_model_from_omission() -> None:
    """PATCH null clears a model while omission preserves the current model."""

    omitted = AgentUpdateRequest.model_validate({"description": "updated"})
    assert "model" not in omitted.model_fields_set
    cleared = AgentUpdateRequest.model_validate({"model": None})
    assert "model" in cleared.model_fields_set
    assert cleared.model is None


def test_runtime_settings_fields_accept_omission_and_explicit_null() -> None:
    """PATCH settings distinguishes omitted fields from an explicit null update."""

    fields = (
        "search_strategy",
        "self_evolution_enabled",
        "self_evolution_idle_minutes",
        "self_evolution_min_turns",
    )

    omitted = RuntimeSettingsRequest.model_validate({})
    explicit_null = RuntimeSettingsRequest.model_validate({field: None for field in fields})

    assert omitted.model_fields_set == set()
    assert explicit_null.model_fields_set == set(fields)
    assert all(getattr(omitted, field) is None for field in fields)
    assert all(getattr(explicit_null, field) is None for field in fields)


@pytest.mark.asyncio
async def test_validation_errors_use_the_api_error_envelope() -> None:
    """Malformed request bodies must receive the common API error response."""

    app = FastAPI()
    install_error_handlers(app)

    @app.post("/prompt")
    async def prompt(payload: SessionPromptRequest) -> dict[str, str]:
        return {"message_id": payload.message_id}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/prompt",
            json={
                "message_id": "message-1",
                "content": "Explain SSE",
                "extra": 1,
            },
        )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["errors"][0]["loc"] == ["body", "extra"]
    assert payload["error"]["details"]["errors"][0]["type"] == "extra_forbidden"


@pytest.mark.asyncio
async def test_malformed_json_is_a_bad_request_not_a_typed_validation_error() -> None:
    """A syntactically invalid JSON document is an HTTP 400 parse failure."""

    app = FastAPI()
    install_error_handlers(app)

    @app.post("/api/v1/prompt")
    async def prompt(payload: SessionPromptRequest) -> dict[str, str]:
        return {"message_id": payload.message_id}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/prompt",
            content=b'{"message_id":',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "bad_request",
        "message": "Malformed JSON request body.",
        "details": {
            "errors": [
                {
                    "type": "json_invalid",
                    "loc": ["body", 14],
                    "msg": "JSON decode error",
                    "input": {},
                    "ctx": {"error": "Expecting value"},
                }
            ]
        },
    }


@pytest.mark.asyncio
async def test_uncaught_api_errors_are_sanitized_without_changing_console_500s() -> None:
    """Runtime failures use the API envelope and never echo exception secrets."""

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/api/v1/failure")
    async def api_failure() -> None:
        raise RuntimeError("database password=top-secret")

    @app.get("/console-failure")
    async def console_failure() -> None:
        raise RuntimeError("console token=top-secret")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        api_response = await client.get("/api/v1/failure")
        console_response = await client.get("/console-failure")

    assert api_response.status_code == 500
    assert api_response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error.",
            "details": {},
        }
    }
    assert "top-secret" not in api_response.text
    assert console_response.status_code == 500
    assert console_response.text == "Internal Server Error"
    assert console_response.headers["content-type"].startswith("text/plain")


def test_manual_request_bodies_are_exactly_described_in_openapi() -> None:
    """Manual bounded body parsing must remain visible to generated clients."""

    document = _application().openapi()
    assert not any(
        path.startswith("/api/v1/plugin-consoles") for path in document["paths"]
    )

    avatar_body = document["paths"]["/api/v1/agents/{agent_id}/avatar"]["post"]["requestBody"]
    assert avatar_body == {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                    },
                    "additionalProperties": False,
                }
            }
        },
    }


def _application() -> FastAPI:
    class EmptyRuns:
        async def list(self, session_id: str | None = None) -> list[object]:
            return []

        async def get_record(self, request_id: str) -> None:
            return None

    class EmptyPluginRuntime:
        @asynccontextmanager
        async def lease(self):
            yield type("Generation", (), {"mcps": {}})()

    runtime = type(
        "Runtime",
        (),
        {
            "config": type("Config", (), {"language": "en"})(),
            "runs": EmptyRuns(),
            "plugin_runtime": EmptyPluginRuntime(),
        },
    )()
    return create_app(runtime)


@pytest.mark.asyncio
async def test_public_and_admin_placeholders_validate_real_json_bodies() -> None:
    """Declared bodies must fail validation before a placeholder returns 501."""

    transport = httpx.ASGITransport(app=_application())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid_prompt = await client.post(
            "/api/v1/sessions/session-1/prompt",
            json={
                "message_id": "message-1",
                "content": "hi",
                "extra": True,
            },
        )
        invalid_settings = await client.patch(
            "/api/v1/settings",
            json={"runtime": {"agent_max_steps": 51}},
        )
        invalid_admin = await client.post(
            "/api/v1/providers",
            json={"id": "provider-1", "unexpected": True},
        )

    for response in (invalid_prompt, invalid_settings, invalid_admin):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_real_list_queries_are_bounded_and_removed_streams_stay_absent() -> None:
    """List routes stay strict and removed HTTP stream routes stay absent."""

    transport = httpx.ASGITransport(app=_application())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid_page = await client.get("/api/v1/sessions?page=0")
        invalid_size = await client.get("/api/v1/sessions?page_size=101")
        unknown_query = await client.get("/api/v1/sessions?unknown=value")
        removed_messages = await client.get(
            "/api/v1/sessions/session-1/messages?page=0"
        )
        removed_run_events = await client.get("/api/v1/runs/run-1/events?cursor=0")

    for response in (
        invalid_page,
        invalid_size,
        unknown_query,
    ):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    assert removed_messages.status_code == 404
    assert removed_run_events.status_code == 404

