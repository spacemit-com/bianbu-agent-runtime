from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 12
_SEARCH_DEADLINE_SECONDS = 15
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 100


def search_web(arguments: dict[str, Any], *, base_url: str) -> str:
    """Search through the locally managed SearXNG service."""

    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error("query must be a non-empty string")
    query = query.strip()
    try:
        limit = _result_limit(arguments.get("limit"))
    except (TypeError, ValueError):
        return _error(f"limit must be an integer within 1..{_MAX_LIMIT}")

    category = arguments.get("category", "general")
    time_range = arguments.get("time_range", "")
    if category not in ("general", "news"):
        return _error("category must be general or news")
    if time_range not in ("", "day", "month", "year"):
        return _error("time_range must be empty, day, month, or year")

    query_id = hashlib.sha256(query.encode("utf-8")).hexdigest()[:10]
    started = time.monotonic()
    logger.info(
        "web search started query_id=%s query_chars=%d limit=%d provider=searxng",
        query_id,
        len(query),
        limit,
    )

    if _SEARCH_DEADLINE_SECONDS <= 0:
        status = "skipped_budget"
        results: list[dict[str, Any]] = []
    else:
        try:
            hits = _search_with_deadline(query, limit, base_url, category, time_range)
            results = _normalize_results(hits, limit)
        except concurrent.futures.TimeoutError:
            status = "timeout"
            results = []
        except Exception as exc:
            status = _failure_reason(exc)
            results = []
        else:
            status = "success" if results else "no_results"

    duration_ms = round((time.monotonic() - started) * 1000)
    attempt = {
        "backend": "searxng",
        "status": status,
        "duration_ms": duration_ms,
    }
    if results:
        logger.info(
            "web search succeeded query_id=%s duration_ms=%d results=%d",
            query_id,
            duration_ms,
            len(results),
        )
        return json.dumps(
            {
                "success": True,
                "data": {"web": results},
                "meta": {
                    "provider": "bianbu-agent-searxng",
                    "backend": "searxng",
                    "attempts": [attempt],
                },
            },
            ensure_ascii=False,
        )

    logger.error(
        "web search failed query_id=%s status=%s duration_ms=%d",
        query_id,
        status,
        duration_ms,
    )
    return json.dumps(
        {
            "success": False,
            "error": "SearXNG search failed",
            "meta": {
                "provider": "bianbu-agent-searxng",
                "attempts": [attempt],
            },
        },
        ensure_ascii=False,
    )


def _search_with_deadline(
    query: str,
    limit: int,
    base_url: str,
    category: str = "general",
    time_range: str = "",
) -> list[dict[str, Any]]:
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="web-search-searxng",
    )
    try:
        future = pool.submit(_search_backend, query, limit, base_url, category, time_range)
        return future.result(timeout=_SEARCH_DEADLINE_SECONDS)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _search_backend(
    query: str,
    limit: int,
    base_url: str,
    category: str = "general",
    time_range: str = "",
) -> list[dict[str, Any]]:
    endpoint = f"{_searxng_base_url(base_url)}/search"
    response = httpx.get(
        endpoint,
        params={
            "q": query,
            "format": "json",
            "categories": category,
            **({"time_range": time_range} if time_range else {}),
            "language": "auto",
            "safesearch": 0,
        },
        headers={"Accept": "application/json"},
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("SearXNG response does not contain a results list")
    return results[:limit]


def _searxng_base_url(base_url: str) -> str:
    value = str(base_url).strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("searxng_url must be an HTTP(S) URL")
    return value.rstrip("/")


def _normalize_results(
    hits: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url = str(hit.get("url") or hit.get("href") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append(
            {
                "title": str(hit.get("title") or "").strip(),
                "url": url,
                "description": str(
                    hit.get("content")
                    or hit.get("description")
                    or hit.get("snippet")
                    or hit.get("body")
                    or ""
                ).strip(),
                "position": len(results) + 1,
                **({"published_date": str(hit["publishedDate"])} if hit.get("publishedDate") else {}),
                **({"source": str(hit["source"])} if hit.get("source") else {}),
            }
        )
        if len(results) >= limit:
            break
    return results


def _result_limit(value: Any) -> int:
    if value in (None, ""):
        return _DEFAULT_LIMIT
    if isinstance(value, bool):
        raise TypeError
    parsed = int(value)
    if not 1 <= parsed <= _MAX_LIMIT:
        raise ValueError
    return parsed


def _failure_reason(error: Exception) -> str:
    detail = f"{type(error).__name__} {error}".lower()
    if "timed out" in detail or "timeout" in detail:
        return "timeout"
    if "429" in detail or "rate limit" in detail:
        return "rate_limited"
    if "connect" in detail or "network" in detail:
        return "connection_error"
    if "json" in detail or "results list" in detail:
        return "invalid_response"
    return "request_error"


def _error(message: str) -> str:
    return json.dumps(
        {"success": False, "error": message},
        ensure_ascii=False,
    )
