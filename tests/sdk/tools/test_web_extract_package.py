from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from bianbu_agent._internal.importing import load_package_module


def _load_module(name: str):
    root = Path(__file__).resolve().parents[3] / "bianbu_agent/tools/web"
    return load_package_module(
        root,
        "web_extract" if name == "tool" else name,
        namespace=f"_test_web_extract_{uuid4().hex}",
    )


@pytest.mark.asyncio
async def test_extract_web_content_preserves_html_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _load_module("web")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>Example Article</title>"
                "<style>hidden css</style></head><body>"
                "<main><h1>Heading</h1><p>Hello <b>world</b>.</p></main>"
                "<script>hidden script</script></body></html>"
            ),
            request=request,
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(web, "is_safe_url", lambda _url: True)
    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    payload = json.loads(
        await web.extract_web_content(
            {"urls": ["https://example.test/article"]}
        )
    )

    assert payload["success"] is True
    assert payload["results"][0]["title"] == "Example Article"
    assert "Heading" in payload["results"][0]["content"]
    assert "Hello world." in payload["results"][0]["content"]
    assert "hidden" not in payload["results"][0]["content"]


@pytest.mark.asyncio
async def test_extract_web_content_preserves_private_url_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _load_module("web")
    monkeypatch.setattr(web, "is_safe_url", lambda _url: False)

    payload = json.loads(
        await web.extract_web_content({"urls": ["http://127.0.0.1/private"]})
    )

    assert payload["success"] is False
    assert "private" in payload["results"][0]["error"]


@pytest.mark.asyncio
async def test_extract_web_content_rejects_redirects_to_private_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _load_module("web")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://example.test/article")
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private"},
            request=request,
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        web,
        "is_safe_url",
        lambda url: "127.0.0.1" not in url,
    )
    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    payload = json.loads(
        await web.extract_web_content(
            {"urls": ["https://example.test/article"]}
        )
    )

    assert payload["success"] is False
    assert "private" in payload["results"][0]["error"]


@pytest.mark.asyncio
async def test_extract_web_content_preserves_char_limit_validation() -> None:
    web = _load_module("web")

    payload = json.loads(
        await web.extract_web_content(
            {"urls": ["https://example.test"], "char_limit": 999}
        )
    )

    assert payload["success"] is False
    assert "char_limit" in payload["error"]


@pytest.mark.asyncio
async def test_extract_web_content_isolates_malformed_url_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _load_module("web")

    async def fetch_page(url, *, client, char_limit, requested_format):
        del client, char_limit, requested_format
        return {"url": url, "title": "Valid", "content": "ok", "error": ""}

    monkeypatch.setattr(web, "_fetch_page", fetch_page)

    payload = json.loads(
        await web.extract_web_content(
            {"urls": ["http://[", "https://valid.example"]}
        )
    )

    assert payload["success"] is True
    assert [item["url"] for item in payload["results"]] == [
        "http://[",
        "https://valid.example",
    ]
    assert "Invalid IPv6 URL" in payload["results"][0]["error"]
    assert payload["results"][1]["content"] == "ok"


@pytest.mark.asyncio
async def test_extract_web_content_parsing_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _load_module("web")
    real_feed = web._PageTextParser.feed
    parse_interval: list[float] = []
    ticks: list[float] = []
    stop_heartbeat = False

    def slow_feed(parser, data):
        parse_interval.append(time.monotonic())
        time.sleep(0.1)
        real_feed(parser, data)
        parse_interval.append(time.monotonic())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><body><p>content</p></body></html>",
            request=request,
        )

    async def heartbeat() -> None:
        while not stop_heartbeat:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.002)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(web, "is_safe_url", lambda _url: True)
    monkeypatch.setattr(web._PageTextParser, "feed", slow_feed)
    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    heartbeat_task = asyncio.create_task(heartbeat())

    try:
        await web.extract_web_content({"urls": ["https://example.test"]})
    finally:
        stop_heartbeat = True
        await heartbeat_task

    parse_started, parse_finished = parse_interval
    assert any(parse_started < tick < parse_finished for tick in ticks)


@pytest.mark.asyncio
async def test_extract_web_content_fetches_urls_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _load_module("web")
    both_started = asyncio.Event()
    started: list[str] = []

    async def fetch_page(url, *, client, char_limit, requested_format):
        del client, char_limit, requested_format
        started.append(url)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.2)
        return {"url": url, "title": url, "content": "ok", "error": ""}

    monkeypatch.setattr(web, "_fetch_page", fetch_page)

    value = await web.extract_web_content(
        {"urls": ["https://one.example", "https://two.example"]}
    )

    assert [item["url"] for item in json.loads(value)["results"]] == [
        "https://one.example",
        "https://two.example",
    ]


@pytest.mark.asyncio
async def test_extract_web_content_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _load_module("web")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fetch_page(url, *, client, char_limit, requested_format):
        del url, client, char_limit, requested_format
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(web, "_fetch_page", fetch_page)
    task = asyncio.create_task(
        web.extract_web_content({"urls": ["https://slow.example"]})
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_extract_web_content_cancels_siblings_after_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _load_module("web")
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def fetch_page(url, *, client, char_limit, requested_format):
        del client, char_limit, requested_format
        if url == "https://failure.example":
            await sibling_started.wait()
            raise RuntimeError("unexpected failure")
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    monkeypatch.setattr(web, "_fetch_page", fetch_page)

    with pytest.raises(BaseExceptionGroup) as raised:
        await web.extract_web_content(
            {"urls": ["https://failure.example", "https://sibling.example"]}
        )
    assert any(
        isinstance(error, RuntimeError) and str(error) == "unexpected failure"
        for error in raised.value.exceptions
    )
    await asyncio.wait_for(sibling_cancelled.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_extract_web_content_enforces_whole_page_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = _load_module("web")

    async def fetch_page(url, *, client, char_limit, requested_format):
        del url, client, char_limit, requested_format
        await asyncio.Event().wait()

    monkeypatch.setattr(web, "_fetch_page", fetch_page)
    monkeypatch.setattr(web, "_PAGE_TIMEOUT_SECONDS", 0.01)

    value = await web.extract_web_content(
        {"urls": ["https://slow.example"]}
    )

    assert json.loads(value) == {
        "success": False,
        "results": [
            {
                "url": "https://slow.example",
                "title": "",
                "content": "",
                "error": "request timed out after 0.01 seconds",
            }
        ],
    }


@pytest.mark.asyncio
async def test_tool_waits_for_async_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_module = _load_module("tool")

    async def extract_web_content(arguments):
        await asyncio.sleep(0)
        return json.dumps({"arguments": arguments})

    monkeypatch.setattr(tool_module, "extract_web_content", extract_web_content)
    tool = tool_module.create_tool(SimpleNamespace())

    value = await tool.ainvoke(
        {"urls": ["https://example.com"], "char_limit": 6000}
    )

    assert json.loads(value) == {
        "arguments": {
            "urls": ["https://example.com"],
            "char_limit": 6000,
        }
    }
