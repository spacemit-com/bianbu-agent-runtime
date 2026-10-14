"""`/api/v1/local-models` is a read-only view over the Gateway-owned models."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from bianbu_agent.api import local_models


INTENT_BASE_URL = "http://127.0.0.1:8765"
GATEWAY_BASE_URL = "http://127.0.0.1:18790"
#: Captured before the fixture patches ``httpx.AsyncClient`` so the test's own
#: client keeps talking to the Runtime app instead of the upstream stubs.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _intent_app(models: object, status: int = 200) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/models")
    async def describe() -> object:
        if status != 200:
            raise HTTPException(status, "intent failed")
        return models

    return app


def _gateway_app(
    *,
    llm: object = (),
    embed: object = (),
    status: int = 200,
) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/{domain}/models")
    async def models(domain: str) -> object:
        assert domain in {"llm", "embed"}
        if status != 200:
            raise HTTPException(status, "gateway failed")
        return llm if domain == "llm" else embed

    return app


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch):
    """Route the runtime client at in-process upstream stubs."""

    apps: dict[str, FastAPI] = {}
    real_client = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        base_url = str(kwargs.get("base_url") or "")
        kwargs.pop("base_url", None)
        app = apps[GATEWAY_BASE_URL] if GATEWAY_BASE_URL in base_url else apps[INTENT_BASE_URL]
        return real_client(transport=httpx.ASGITransport(app=app), base_url=base_url)

    monkeypatch.setattr(local_models.httpx, "AsyncClient", factory)
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", INTENT_BASE_URL)
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", GATEWAY_BASE_URL)

    def install(intent: FastAPI, gateway: FastAPI) -> None:
        apps[INTENT_BASE_URL] = intent
        apps[GATEWAY_BASE_URL] = gateway

    return install


async def _describe(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/api/v1/local-models")


def _application() -> FastAPI:
    app = FastAPI()
    app.include_router(local_models.router, prefix="/api/v1")
    return app


def _client() -> httpx.AsyncClient:
    return _REAL_ASYNC_CLIENT(
        transport=httpx.ASGITransport(app=_application()), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_intent_report_is_read_only_and_drops_lifecycle_actions(upstream) -> None:
    upstream(
        _intent_app(
            {
                "models": [
                    {
                        "model_type": "llm",
                        "name": "bibit-llm",
                        "backend": "ai_gateway",
                        "port": None,
                        "download": {"status": "completed", "progress": 100},
                        "server": {"status": "running", "managed": True},
                        "registered": True,
                    },
                    {
                        "model_type": "embed",
                        "name": "bibit-embed",
                        "backend": "ai_gateway",
                        "port": None,
                        "download": {"status": "idle", "progress": 0},
                        "server": {"status": "stopped", "managed": True},
                        "registered": True,
                    },
                ],
                "actions": ["models.download", "models.start", "models.stop"],
            }
        ),
        _gateway_app(),
    )
    async with _client() as client:
        response = await _describe(client)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "bibit-intent"
    assert data["read_only"] is True
    assert "actions" not in data
    assert [model["model_type"] for model in data["models"]] == ["llm", "embed"]
    assert data["models"][0]["server"] == {"status": "running", "managed": True}
    assert data["models"][1]["download"] == {"status": "idle", "progress": 0}
    # Intent reports Gateway ids only; without a Gateway row there is no artifact.
    assert [model["artifact"] for model in data["models"]] == [None, None]


@pytest.mark.asyncio
async def test_intent_ids_gain_the_concrete_model_of_the_device(upstream) -> None:
    """``bibit-llm``/``bibit-embed`` name the Gateway entries, not the files."""

    upstream(
        _intent_app(
            {
                "models": [
                    {
                        "model_type": "llm",
                        "name": "bibit-llm",
                        "backend": "ai_gateway",
                        "port": None,
                        "download": {"status": "completed", "progress": 100},
                        "server": {"status": "running", "managed": True},
                    },
                    {
                        "model_type": "embed",
                        "name": "bibit-embed",
                        "backend": "ai_gateway",
                        "port": None,
                        "download": {"status": "completed", "progress": 100},
                        "server": {"status": "running", "managed": True},
                    },
                ]
            }
        ),
        _gateway_app(
            llm=[
                {
                    "id": "bibit-llm",
                    "source_type": "local_path",
                    "local_path": "/home/bianbu/.cache/models/llm/glm-edge-1.5b-chat-q4_0.gguf",
                    "status": "loaded",
                }
            ],
            embed=[
                {
                    "id": "bibit-embed",
                    "source_type": "local_path",
                    "local_path": "/home/bianbu/.cache/models/embed/Qwen3-Embedding-0.6B-Q4_0.gguf",
                    "status": "loaded",
                }
            ],
        ),
    )
    async with _client() as client:
        response = await _describe(client)

    assert response.status_code == 200
    data = response.json()["data"]
    llm, embed = data["models"]
    assert [llm["name"], llm["artifact"]] == ["bibit-llm", "glm-edge-1.5b-chat-q4_0.gguf"]
    assert llm["path"] == "/home/bianbu/.cache/models/llm/glm-edge-1.5b-chat-q4_0.gguf"
    assert embed["artifact"] == "Qwen3-Embedding-0.6B-Q4_0.gguf"


@pytest.mark.asyncio
async def test_intent_report_survives_an_unavailable_gateway(upstream) -> None:
    """Resolving the artifact is best-effort; Intent state still reaches the UI."""

    upstream(
        _intent_app(
            {
                "models": [
                    {
                        "model_type": "llm",
                        "name": "bibit-llm",
                        "backend": "ai_gateway",
                        "port": None,
                        "download": {"status": "completed", "progress": 100},
                        "server": {"status": "running", "managed": True},
                    }
                ]
            }
        ),
        _gateway_app(status=500),
    )
    async with _client() as client:
        response = await _describe(client)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "bibit-intent"
    assert [model["artifact"] for model in data["models"]] == [None]


@pytest.mark.asyncio
async def test_gateway_report_is_used_when_intent_is_unavailable(upstream) -> None:
    upstream(
        _intent_app({}, status=503),
        _gateway_app(
            llm=[
                {
                    "id": "preset-not-downloaded",
                    "source_type": "local_url",
                    "url": "https://archive.spacemit.com/llm.gguf",
                    "status": "available",
                    "download_progress": 0.0,
                },
                {
                    "id": "bibit-llm",
                    "source_type": "local_url",
                    "url": "https://archive.spacemit.com/bibit-llm.gguf",
                    "status": "loaded",
                },
            ],
            embed=[
                {
                    "id": "bibit-embed",
                    "source_type": "local_path",
                    "local_path": "/opt/bibit-intent/models/bibit-embed.gguf",
                    "status": "downloaded",
                }
            ],
        ),
    )
    async with _client() as client:
        response = await _describe(client)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "ai-gateway"
    assert data["read_only"] is True
    assert [model["name"] for model in data["models"]] == ["bibit-llm", "bibit-embed"]
    assert data["models"][0]["server"]["status"] == "running"
    assert data["models"][1]["server"]["status"] == "stopped"
    assert data["models"][1]["path"] == "/opt/bibit-intent/models/bibit-embed.gguf"
    assert data["models"][0]["download"] == {"status": "completed", "progress": 100}
    assert [model["artifact"] for model in data["models"]] == ["bibit-llm.gguf", "bibit-embed.gguf"]


@pytest.mark.asyncio
async def test_both_sources_unavailable_reports_each_failure(upstream) -> None:
    upstream(_intent_app({}, status=503), _gateway_app(status=500))
    async with _client() as client:
        response = await _describe(client)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "bad_gateway"
    assert "Bibit Intent request failed" in detail["message"]
    assert "AI Gateway request failed" in detail["message"]


@pytest.mark.asyncio
async def test_gateway_without_local_models_is_an_error(upstream) -> None:
    upstream(_intent_app({"models": []}), _gateway_app())
    async with _client() as client:
        response = await _describe(client)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "bad_gateway"
    assert "no local models" in detail["message"]
