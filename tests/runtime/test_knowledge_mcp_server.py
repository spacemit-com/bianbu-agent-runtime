from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).parents[2]
SERVER = ROOT / "resources/mcps/spacemit-knowledge/server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("spacemit_knowledge_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _chunk(index: int, **overrides):
    value = {
        "id": str(index),
        "content": "内容",
        "document_id": f"doc-{index}",
        "document_keyword": "手册",
        "similarity": 0.9,
        "url": "https://example.com/manual",
    }
    value.update(overrides)
    return value


def test_arguments_are_strict_and_trimmed():
    server = _load_server()
    assert server._parse_arguments({"query": "  Bianbu 内核  "}) == "Bianbu 内核"
    for value in (
        {},
        {"query": "  "},
        {"query": 1},
        {"query": "x", "intent": "rewrite"},
        {"queries": ["x"]},
        {"query": "字" * 16001},
    ):
        with pytest.raises(ValueError):
            server._parse_arguments(value)


def test_normalize_distinguishes_success_empty_and_malformed():
    server = _load_server()
    output = server._normalize(
        "question", {"code": 0, "data": {"chunks": [_chunk(1)]}}
    )
    assert output["status"] == "succeeded"
    assert output["chunks"][0]["documentName"] == "手册"
    assert output["total"] == 1

    empty = server._normalize("question", {"code": 0, "data": {"chunks": []}})
    assert empty["status"] == "empty"
    assert empty["forumUrl"] == server.FORUM_URL

    for body in ({}, {"code": 101, "data": {"chunks": []}},
                 {"code": 0, "data": {"chunks": [{}]}}):
        with pytest.raises(server.KnowledgeError):
            server._normalize("question", body)


def test_normalize_bounds_deduplicates_and_filters_sources():
    server = _load_server()
    items = [_chunk(0), _chunk(0)]
    items.extend(_chunk(index) for index in range(1, 20))
    result = server._normalize("q", {"code": 0, "data": {"chunks": items}})
    assert result["total"] == server.MAX_CHUNKS
    chunks = result["chunks"]
    assert len({(chunk["documentId"], chunk["content"]) for chunk in chunks}) == len(
        chunks
    )

    large = _chunk(0, content="😀" * 10000, document_keyword="名" * 600)
    items = [dict(large, document_id=str(index)) for index in range(8)]
    result = server._normalize("q", {"code": 0, "data": {"chunks": items}})
    chunks = result["chunks"]
    assert len(chunks) == 4
    assert sum(len(chunk["content"]) for chunk in chunks) == server.MAX_CONTENT_CHARS
    assert len(chunks[0]["documentName"]) == 300

    for url in (
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://user:secret@example.com",
        "https://example.com?access_token=secret",
        "invalid",
    ):
        assert server._source_url(url) is None
    assert (
        server._source_url("https://example.com/manual?id=1")
        == "https://example.com/manual?id=1"
    )


def test_tool_result_bounds_the_complete_payload_and_marks_errors():
    server = _load_server()
    items = [
        _chunk(
            index,
            content="文😀" * 4000,
            id="\x01" * 256,
            document_id=f"{index}" + "\x01" * 255,
            document_keyword="\x01" * 300,
            url="https://example.com/" + "x" * 2000,
        )
        for index in range(8)
    ]
    output = server._normalize(
        "q" * 16000, {"code": 0, "data": {"chunks": items}}
    )
    result = server._tool_result(output)
    serialized = json.dumps(
        result.structuredContent, ensure_ascii=False, separators=(",", ":")
    )
    assert len(serialized) <= server.MAX_RESULT_CHARS
    assert result.isError is False
    assert len(result.structuredContent["question"]) == 512
    assert result.structuredContent["total"] == len(
        result.structuredContent["chunks"]
    )
    assert result.content[0].text == serialized

    error = server._tool_result(
        server._error_result("q", "network", True)
    )
    assert error.isError is True
    assert error.structuredContent["status"] == "unavailable"


def test_failure_reasons_do_not_echo_private_details():
    server = _load_server()
    assert server._failure_reason("knowledge_timeout") == "timeout"
    assert server._failure_reason("knowledge_network_error") == "network"
    for status in (500, 502, 503, 504):
        assert (
            server._failure_reason(f"RAGFlow returned HTTP {status}")
            == "service_unavailable"
        )
    assert (
        server._failure_reason("RAGFlow returned HTTP 429 Too Many Requests")
        == "rate_limited"
    )
    assert server._failure_reason("RAGFlow returned HTTP 400") == "service_error"
    assert (
        server._failure_reason("invalid RAGFlow response: invalid JSON")
        == "invalid_response"
    )
    safe = server._failure_reason("https://user:secret@private.invalid")
    assert safe == "service_error"


@pytest.mark.asyncio
async def test_search_maps_retrieval_failures(monkeypatch):
    server = _load_server()

    async def unavailable(_question):
        raise server.KnowledgeError("knowledge_timeout")

    monkeypatch.setattr(server, "_retrieve", unavailable)
    result = await server._search({"query": "Bianbu"})
    assert result.isError is True
    assert result.structuredContent["status"] == "unavailable"
    assert result.structuredContent["error"] == "timeout"

    result = await server._search({})
    assert result.isError is True
    assert result.structuredContent["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_stdio_server_lists_the_knowledge_tool():
    parameters = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
    assert [tool.name for tool in tools.tools] == ["spacemit_knowledge_search"]
    tool = tools.tools[0]
    assert tool.inputSchema["required"] == ["query"]
    assert tool.inputSchema["additionalProperties"] is False
    assert tool.annotations.readOnlyHint is True
