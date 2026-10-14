from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from bianbu_agent.tools.builtins import load_builtin, builtin_source

from bianbu_agent import ToolCallContext
from bianbu_agent.tools import DirectoryToolLoader, ToolLoadContext


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_web_search_always_uses_local_searxng(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, Any] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "results": [
                    {
                        "title": "Local result",
                        "url": "https://example.test/result",
                        "content": "from local SearXNG",
                    }
                ]
            }

    def get(url: str, **kwargs: Any) -> Response:
        received["url"] = url
        received.update(kwargs)
        return Response()

    monkeypatch.setenv(
        "BIANBU_AGENT_SEARXNG_URL",
        "http://must-not-be-used.invalid:9999",
    )
    monkeypatch.setattr(httpx, "get", get)
    contribution = load_builtin(
        "web_search",
        context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
    )

    result = await contribution.handler(
        ToolCallContext("run", "session", "agent"),
        {"query": "local search", "limit": 3},
    )

    payload = json.loads(result.content)
    assert payload["success"] is True
    assert received["url"] == "http://127.0.0.1:8888/search"
    assert received["params"] == {
        "q": "local search",
        "format": "json",
        "categories": "general",
        "language": "auto",
        "safesearch": 0,
    }


@pytest.mark.asyncio
async def test_web_search_does_not_block_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"results": []}

    def get(*_args: Any, **_kwargs: Any) -> Response:
        time.sleep(0.2)
        return Response()

    monkeypatch.setattr(httpx, "get", get)
    contribution = load_builtin(
        "web_search",
        context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
    )
    started = time.monotonic()
    task = asyncio.create_task(
        contribution.handler(
            ToolCallContext("run", "session", "agent"), {"query": "slow"}
        )
    )

    await asyncio.sleep(0.02)
    assert time.monotonic() - started < 0.1
    await task


@pytest.mark.asyncio
async def test_news_search_preserves_dates_and_requests_news_category(tmp_path, monkeypatch):
    received = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"title": "AI release", "url": "https://example.test/news",
                "content": "Announcement", "publishedDate": "2026-09-08T10:00:00",
                "source": "Example News"}]}

    def get(url, **kwargs):
        received.update(kwargs)
        return Response()

    monkeypatch.setattr(httpx, "get", get)
    contribution = load_builtin(
        "web_search",
        context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
    )
    result = await contribution.handler(ToolCallContext("run", "session", "agent"),
        {"query": "AI news", "category": "news", "time_range": "month"})
    payload = json.loads(result.content)
    assert received["params"]["categories"] == "news"
    assert received["params"]["time_range"] == "month"
    assert payload["data"]["web"][0]["published_date"] == "2026-09-08T10:00:00"
    assert payload["data"]["web"][0]["source"] == "Example News"
