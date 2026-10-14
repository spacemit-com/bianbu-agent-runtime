from __future__ import annotations

from types import SimpleNamespace

from fastapi.routing import APIRoute, APIWebSocketRoute

from bianbu_agent.server.app import create_app


def _registered_api_operations(routes, *, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten FastAPI's lazy included routers without losing duplicate routes."""

    operations: list[tuple[str, str]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            path = prefix + route.path
            if path.startswith("/api/"):
                operations.extend(
                    (method, path)
                    for method in route.methods
                    if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                )
            continue
        child = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        if child is not None and context is not None:
            operations.extend(
                _registered_api_operations(
                    child.routes,
                    prefix=prefix + context.prefix,
                )
            )
    return operations


def _registered_websockets(routes, *, prefix: str = "") -> list[str]:
    paths: list[str] = []
    for route in routes:
        if isinstance(route, APIWebSocketRoute):
            paths.append(prefix + route.path)
            continue
        child = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        if child is not None and context is not None:
            paths.extend(
                _registered_websockets(
                    child.routes,
                    prefix=prefix + context.prefix,
                )
            )
    return paths


def test_application_exposes_exact_versioned_api_inventory() -> None:
    """The old flat API must not survive alongside the resource API."""

    app = create_app(SimpleNamespace(config=SimpleNamespace(language="en")))
    operations = _registered_api_operations(app.routes)

    expected_operations = {
        ("POST", "/api/v1/sessions/{session_id}/prompt"),
        ("POST", "/api/v1/sessions/{session_id}/generate-title"),
        ("POST", "/api/v1/sessions/{session_id}/cancel"),
        ("POST", "/api/v1/sessions/{session_id}/approvals/{approval_id}"),
        ("GET", "/api/v1/sessions"),
        ("POST", "/api/v1/sessions"),
        ("GET", "/api/v1/sessions/search"),
        ("GET", "/api/v1/sessions/{session_id}"),
        ("PATCH", "/api/v1/sessions/{session_id}"),
        ("DELETE", "/api/v1/sessions/{session_id}"),
        ("POST", "/api/v1/sessions/{session_id}/clear-context"),
        ("GET", "/api/v1/sessions/{session_id}/events"),
        ("GET", "/api/v1/sessions/{session_id}/history"),
        ("GET", "/api/v1/sessions/{session_id}/vision"),
        ("POST", "/api/v1/sessions/{session_id}/attachments"),
        ("GET", "/api/v1/sessions/{session_id}/attachments/{name}"),
        ("GET", "/api/v1/agents"),
        ("GET", "/api/v1/agents/{agent_id}"),
        ("GET", "/api/v1/settings"),
        ("PATCH", "/api/v1/settings"),
        ("GET", "/api/v1/workspace/download"),
        ("POST", "/api/v1/agents"),
        ("PATCH", "/api/v1/agents/{agent_id}"),
        ("POST", "/api/v1/agents/{agent_id}/restart"),
        ("DELETE", "/api/v1/agents/{agent_id}"),
        ("POST", "/api/v1/agents/{agent_id}/avatar"),
        ("GET", "/api/v1/agents/{agent_id}/avatar"),
        ("DELETE", "/api/v1/agents/{agent_id}/avatar"),
        ("POST", "/api/v1/agents/{agent_id}/executions"),
        ("POST", "/api/v1/agents/{agent_id}/revoke"),
        ("DELETE", "/api/v1/auth/session"),
        ("GET", "/api/v1/auth/session"),
        ("POST", "/api/v1/auth/session"),
        ("GET", "/api/v1/executions"),
        ("GET", "/api/v1/executions/{instance_id}"),
        ("POST", "/api/v1/executions/{instance_id}/stop"),
        ("GET", "/api/v1/providers"),
        ("POST", "/api/v1/providers"),
        ("GET", "/api/v1/providers/{provider_id}"),
        ("PATCH", "/api/v1/providers/{provider_id}"),
        ("DELETE", "/api/v1/providers/{provider_id}"),
        ("GET", "/api/v1/providers/{provider_id}/models"),
        ("GET", "/api/v1/capabilities/{kind}"),
        ("GET", "/api/v1/mcp-servers"),
        ("POST", "/api/v1/mcp-servers"),
        ("DELETE", "/api/v1/mcp-servers"),
        ("POST", "/api/v1/mcp-health-checks"),
        ("GET", "/api/v1/packages/{kind}"),
        ("POST", "/api/v1/packages/{kind}"),
        ("GET", "/api/v1/packages/{kind}/{package_id}"),
        ("POST", "/api/v1/packages/{kind}/{package_id}/verify"),
        ("DELETE", "/api/v1/packages/{kind}/{package_id}"),
        ("GET", "/api/v1/plugins"),
        ("POST", "/api/v1/plugins"),
        ("GET", "/api/v1/plugins/effective"),
        ("GET", "/api/v1/plugins/{plugin_id}"),
        ("POST", "/api/v1/plugins/{plugin_id}/rollback"),
        ("DELETE", "/api/v1/plugins/{plugin_id}"),
        ("GET", "/api/v1/local-models"),
        ("GET", "/api/v1/logs"),
        ("GET", "/api/v1/operational-logs"),
        ("GET", "/api/v1/provider-capture"),
        ("PUT", "/api/v1/provider-capture"),
        ("DELETE", "/api/v1/provider-capture/target"),
        ("GET", "/api/v1/provider-capture/{record_id}"),
        ("DELETE", "/api/v1/provider-capture/records"),
        ("GET", "/api/v1/agent-top"),
        ("GET", "/api/v1/executions"),
        ("GET", "/api/v1/executions/{instance_id}"),
        ("POST", "/api/v1/executions/{instance_id}/stop"),
        ("GET", "/api/v1/runtime/status"),
        ("GET", "/api/v1/runtime/maintenance"),
        ("GET", "/api/v1/runtime/workers"),
        ("GET", "/api/v1/security/syscalls/catalog"),
        ("GET", "/api/v1/security/agents/{agent_id}/syscall-policy"),
        ("POST", "/api/v1/security/agents/{agent_id}/syscall-policy"),
        ("DELETE", "/api/v1/security/agents/{agent_id}/syscall-policy"),
        ("GET", "/api/v1/security/syscall-audit/events"),
        ("GET", "/api/v1/security/syscall-audit/status"),
        ("GET", "/api/health"),
    }

    assert len(operations) == len(set(operations))
    assert set(operations) == expected_operations

    assert not any(path == "/api/v1/runs" or path.startswith("/api/v1/runs/") for _, path in operations)
    assert _registered_websockets(app.routes) == [
        "/api/v1/events/mux",
        "/api/v1/events/host",
    ]
    assert not any(path.endswith("/event-stream") for _, path in operations)
    assert not any("/messages" in path for _, path in operations)
    assert ("PATCH", "/api/v1/settings") in operations
    assert not any(path.startswith("/api/admin/") for _, path in operations)
    assert not any(path == "/api/v1/models" or path.startswith("/api/v1/models/") for _, path in operations)
    assert not any(path == "/api/v1/capability-bindings" or path.startswith("/api/v1/capability-bindings/") for _, path in operations)
    assert not any(path == "/api/v1/files" or path.startswith("/api/v1/files/") for _, path in operations)
    assert not any(path == "/api/v1/avatars" or path.startswith("/api/v1/avatars/") for _, path in operations)
    assert ("GET", "/api/health") in operations
    assert ("POST", "/api/message") not in operations


def test_session_prompt_route_is_steer_only() -> None:
    app = create_app(SimpleNamespace(config=SimpleNamespace(language="en")))

    document = app.openapi()
    operation = document["paths"]["/api/v1/sessions/{session_id}/prompt"]["post"]
    schema = document["components"]["schemas"]["SessionPromptRequest"]

    assert operation.get("deprecated") is not True
    assert set(schema["required"]) == {"message_id", "content"}
    assert "mode" not in schema["properties"]
    assert "/api/v1/runs/{run_id}/steer" not in document["paths"]


def test_mcp_resources_have_exact_typed_openapi_responses() -> None:
    document = create_app(SimpleNamespace(config=SimpleNamespace(language="en"))).openapi()
    collection_ref = {
        "$ref": "#/components/schemas/MCPServerCollectionEnvelope"
    }

    assert document["paths"]["/api/v1/mcp-servers"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"] == collection_ref
    health_operation = document["paths"]["/api/v1/mcp-health-checks"]["post"]
    assert health_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == collection_ref
    assert "requestBody" not in health_operation

    schemas = document["components"]["schemas"]
    server = schemas["MCPServerView"]
    assert server["additionalProperties"] is False
    assert set(server["required"]) == {
        "id",
        "display_name",
        "command",
        "kind",
        "status",
        "transport",
        "tools",
        "source_kind",
        "owner_plugin_id",
        "removable",
        "health_checked_at",
    }
    assert server["properties"]["tools"]["items"] == {
        "$ref": "#/components/schemas/MCPServerToolView"
    }
    assert server["properties"]["health_checked_at"]["anyOf"] == [
        {"type": "string", "format": "date-time"},
        {"type": "null"},
    ]
