from __future__ import annotations

import asyncio
import json
import re
from html.parser import HTMLParser
from typing import Any

import httpx
from .url_safety import (
    is_safe_url,
    normalize_url_for_request,
    sensitive_query_param_name,
)

_MAX_URLS = 10
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_DEFAULT_CHAR_LIMIT = 15_000
_PAGE_TIMEOUT_SECONDS = 20.0
_IGNORED_TAGS = {"script", "style", "noscript", "svg", "template"}
_BLOCK_TAGS = {
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "nav",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
}


class _PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self._parts: list[str] = []
        self._title_parts: list[str] = []

    @property
    def title(self) -> str:
        return _normalize_inline(" ".join(self._title_parts))

    @property
    def text(self) -> str:
        lines = [_normalize_inline(line) for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line)

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if normalized == "title":
            self._in_title = True
        if normalized in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in _IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if normalized == "title":
            self._in_title = False
        if normalized in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._parts.append(data)


async def extract_web_content(arguments: dict[str, Any]) -> str:
    """Fetch public URLs directly and return clean text as JSON."""

    values = arguments.get("urls")
    if not isinstance(values, list) or not values:
        return _result_error("urls must be a non-empty array")
    if len(values) > _MAX_URLS:
        return _result_error(f"at most {_MAX_URLS} URLs can be extracted")
    try:
        char_limit = _char_limit(arguments.get("char_limit"))
    except (TypeError, ValueError):
        return _result_error("char_limit must be an integer within 1000..100000")
    requested_format = str(arguments.get("format") or "markdown").lower()
    if requested_format not in {"markdown", "text", "html"}:
        return _result_error("format must be markdown, text, or html")

    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=5,
        timeout=httpx.Timeout(_PAGE_TIMEOUT_SECONDS),
        headers={"User-Agent": "BianbuAgent/0.1 (+web_extract)"},
        event_hooks={"request": [_validate_request]},
    ) as client:
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(
                    _extract_page(
                        value,
                        client=client,
                        char_limit=char_limit,
                        requested_format=requested_format,
                    )
                )
                for value in values
            ]
        results = [task.result() for task in tasks]
    return json.dumps(
        {"success": any(not item["error"] for item in results), "results": results},
        ensure_ascii=False,
    )


async def _extract_page(
    value: Any,
    *,
    client: httpx.AsyncClient,
    char_limit: int,
    requested_format: str,
) -> dict[str, str]:
    url = _item_url(value)
    if not url:
        return _page_error("", "expected a URL string")
    normalized = url
    try:
        normalized = normalize_url_for_request(url)
        sensitive_key = sensitive_query_param_name(normalized)
        if sensitive_key:
            return _page_error(
                normalized,
                f"URL contains a credential-like query parameter: {sensitive_key}",
            )
        async with asyncio.timeout(_PAGE_TIMEOUT_SECONDS):
            return await _fetch_page(
                normalized,
                client=client,
                char_limit=char_limit,
                requested_format=requested_format,
            )
    except TimeoutError:
        return _page_error(
            normalized,
            f"request timed out after {_PAGE_TIMEOUT_SECONDS:g} seconds",
        )
    except (httpx.HTTPError, UnicodeError, ValueError) as error:
        return _page_error(normalized, str(error))


async def _fetch_page(
    url: str,
    *,
    client: httpx.AsyncClient,
    char_limit: int,
    requested_format: str,
) -> dict[str, str]:
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        payload = bytearray()
        async for chunk in response.aiter_bytes():
            payload.extend(chunk)
            if len(payload) > _MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds 5 MiB")
        content_type = response.headers.get("content-type", "").lower()
        encoding = response.encoding or "utf-8"
        final_url = str(response.url)

    return await asyncio.to_thread(
        _parse_payload,
        payload,
        content_type=content_type,
        encoding=encoding,
        final_url=final_url,
        char_limit=char_limit,
        requested_format=requested_format,
    )


def _parse_payload(
    payload: bytearray,
    *,
    content_type: str,
    encoding: str,
    final_url: str,
    char_limit: int,
    requested_format: str,
) -> dict[str, str]:
    text = bytes(payload).decode(encoding, errors="replace")

    title = ""
    if "html" in content_type:
        if requested_format == "html":
            content = text
        else:
            parser = _PageTextParser()
            parser.feed(text)
            parser.close()
            title = parser.title
            content = parser.text
    elif content_type.startswith("text/") or any(
        marker in content_type for marker in ("json", "xml")
    ):
        content = text
    else:
        raise ValueError(f"unsupported content type: {content_type or 'unknown'}")
    return {
        "url": final_url,
        "title": title,
        "content": _truncate(content, char_limit),
        "error": "",
    }


async def _validate_request(request: httpx.Request) -> None:
    await asyncio.to_thread(_require_safe_url, str(request.url))


def _require_safe_url(url: str) -> None:
    if not is_safe_url(url):
        raise ValueError("URL targets a private, internal, or invalid address")


def _item_url(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    return value.strip() if isinstance(value, str) else ""


def _char_limit(value: Any) -> int:
    if value in (None, ""):
        return _DEFAULT_CHAR_LIMIT
    if isinstance(value, bool):
        raise ValueError("char_limit must be an integer")
    parsed = int(value)
    if not 1_000 <= parsed <= 100_000:
        raise ValueError("char_limit must be within 1000..100000")
    return parsed


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    head_size = int(limit * 0.7)
    tail_size = limit - head_size
    return (
        text[:head_size].rstrip()
        + f"\n\n[... {omitted} characters omitted ...]\n\n"
        + text[-tail_size:].lstrip()
    )


def _normalize_inline(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _page_error(url: str, error: str) -> dict[str, str]:
    return {"url": url, "title": "", "content": "", "error": error}


def _result_error(error: str) -> str:
    return json.dumps({"success": False, "error": error}, ensure_ascii=False)
