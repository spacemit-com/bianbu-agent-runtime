from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import asyncio
import re
import shutil

import httpx
import pytest

from bianbu_agent import (
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    RunEvent,
    RuntimeConfig,
)
from bianbu_agent.server import app as app_module
from bianbu_agent.server.app import _console_asset_version, create_app


@dataclass
class Model:
    model: str


class Engine:
    async def stream(self, prompt, **kwargs):
        text = prompt if isinstance(prompt, str) else "".join(item.content for item in prompt)
        yield RunEvent("token", {"text": text})
        yield RunEvent("done", {})


class UsageEngine:
    async def stream(self, prompt, **kwargs):
        text = prompt if isinstance(prompt, str) else "".join(item.content for item in prompt)
        yield RunEvent("token", {"text": text})
        yield RunEvent(
            "usage",
            {"prompt_tokens": 3, "completion_tokens": 2},
        )
        yield RunEvent("done", {})


def _agent_options() -> dict[str, object]:
    agent = AgentSpec(id="default", name="Default")
    return {
        "agent_id": agent.id,
        "agent_registry": InMemoryAgentRegistry((agent,)),
    }


async def _terminal_run(
    runtime: BianbuAgentRuntime,
    run_id: str,
) -> dict[str, object]:
    for _ in range(100):
        record = await runtime.runs.get_record(run_id)
        if record is not None and record.status in {"completed", "failed", "cancelled"}:
            return {
                "status": record.status,
                "content": record.result.content if record.result else "",
                "usage": None if record.result is None or record.result.usage is None else {
                    "input_tokens": record.result.usage.input_tokens,
                    "output_tokens": record.result.usage.output_tokens,
                    "total_tokens": record.result.usage.total_tokens,
                    "metadata": dict(record.result.usage.metadata),
                },
            }
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run did not become terminal: {run_id}")


@pytest.mark.asyncio
async def test_runtime_app_creates_run_and_enforces_session_agent_binding(
    tmp_path: Path,
) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=Model("default"),
        **_agent_options(),
        engine_builder=lambda *args: Engine(),
    )
    await runtime.start()
    await runtime.agents.create(AgentSpec(id="writer", name="Writer"))
    transport = httpx.ASGITransport(
        app=create_app(runtime, manage_lifespan=False)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/sessions",
            json={"id": "chat", "agent_id": "writer"},
        )
        assert created.status_code == 201
        result = await client.post(
            "/api/v1/sessions/chat/prompt",
            json={"message_id": "message-1", "content": "hello"},
        )
        assert result.status_code == 202
        snapshot = await _terminal_run(runtime, result.json()["data"]["run_id"])
        assert snapshot["status"] == "completed"
        assert str(snapshot["content"]).endswith("hello")

        conflict = await client.post(
            "/api/v1/sessions",
            json={"id": "chat", "agent_id": "default"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "session_agent_mismatch"

        override = await client.post(
            "/api/v1/sessions/chat/prompt",
            json={"message_id": "message-2", "content": "no", "model": "other-model"},
        )
        assert override.status_code == 422
        assert "extra_forbidden" in override.text
        assert override.json()["error"]["code"] == "validation_error"
        assert (await client.post("/api/chat", json={})).status_code == 404

    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_chat_serializes_nested_immutable_usage(tmp_path: Path) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=Model("default"),
        **_agent_options(),
        engine_builder=lambda *args: UsageEngine(),
    )
    await runtime.start()
    transport = httpx.ASGITransport(
        app=create_app(runtime, manage_lifespan=False),
        raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/v1/sessions", json={"id": "usage", "agent_id": "default"}
        )
        response = await client.post(
            "/api/v1/sessions/usage/prompt",
            json={"message_id": "usage-1", "content": "hello"},
        )
        assert response.status_code == 202
        snapshot = await _terminal_run(runtime, response.json()["data"]["run_id"])

    assert snapshot["usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "metadata": {"events": 1},
    }
    await runtime.close()


def test_runtime_app_has_one_canonical_api_without_console_prefix(
    tmp_path: Path,
) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=Model("default"),
        **_agent_options(),
        engine_builder=lambda *args: Engine(),
    )
    app = create_app(runtime)
    paths = set(app.openapi()["paths"])
    assert {
        "/api/v1/sessions",
        "/api/v1/agents",
        "/api/v1/settings",
        "/api/v1/providers",
        "/api/v1/providers/{provider_id}/models",
        "/api/v1/mcp-servers",
        "/api/v1/capabilities/{kind}",
        "/api/v1/packages/{kind}",
        "/api/v1/local-models",
    } <= paths
    assert not any(path == "/api/v1/runs" or path.startswith("/api/v1/runs/") for path in paths)
    assert "/api/v1/runs/{run_id}/events" not in paths
    assert all(
        path.startswith("/api/v1/")
        for path in paths
        if path.startswith("/api/")
    )


@pytest.mark.asyncio
async def test_console_html_revalidates_and_versions_first_party_scripts(
    tmp_path: Path,
) -> None:
    """A Runtime upgrade must not reuse JavaScript for an older API contract."""

    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=Model("default"),
        **_agent_options(),
        engine_builder=lambda *args: Engine(),
    )
    app = create_app(runtime, manage_lifespan=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/chat")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache, must-revalidate"
        sources = re.findall(
            r'<script defer src="(assets/js/[^"?]+\?v=([0-9a-f]{16}))"></script>',
            response.text,
        )
        assert [source.rsplit("/", 1)[-1].split("?", 1)[0] for source, _ in sources] == [
            "think_filter.js",
            "session-runtime.js",
            "trajectory.js",
            "console.js",
        ]
        assert len({version for _, version in sources}) == 1
        for source, _ in sources:
            assert (await client.get(f"/{source}")).status_code == 200

        stylesheets = re.findall(
            r'<link rel="stylesheet" href="(assets/css/[^"?]+\?v=([0-9a-f]{16}))">',
            response.text,
        )
        assert [source.split("/")[-1].split("?", 1)[0] for source, _ in stylesheets] == [
            "console.css",
        ]
        assert {version for _, version in stylesheets} == {version for _, version in sources}
        for source, _ in stylesheets:
            assert (await client.get(f"/{source}")).status_code == 200


def test_console_asset_version_follows_a_stylesheet_only_change(tmp_path: Path) -> None:
    """A CSS-only fix must change the URL, or the browser keeps the old styles."""

    static_root = tmp_path / "static"
    shutil.copytree(app_module._WEB_ROOT / "static", static_root)
    baseline = _console_asset_version(static_root)

    stylesheet = static_root / "css" / "console.css"
    stylesheet.write_text(
        stylesheet.read_text(encoding="utf-8") + "\n.mention-hint { opacity: 1; }\n",
        encoding="utf-8",
    )

    assert _console_asset_version(static_root) != baseline


def test_runtime_app_exposes_session_prompt_without_run_steer(tmp_path: Path) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=Model("default"),
        **_agent_options(),
        engine_builder=lambda *args: Engine(),
    )

    paths = create_app(runtime).openapi()["paths"]

    assert "post" in paths["/api/v1/sessions/{session_id}/prompt"]
    assert "post" in paths["/api/v1/sessions/{session_id}/cancel"]
    assert not any(path == "/api/v1/runs" or path.startswith("/api/v1/runs/") for path in paths)
