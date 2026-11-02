"""Isolated model-backed Session title generation."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import replace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from bianbu_agent.engines.default import to_langchain_model
from bianbu_agent.engines.reasoning_markup import strip_reasoning_markup
from bianbu_agent.models import ChatModel, OpenAICompatibleModel
from bianbu_agent.sessions.manager import normalize_session_title


TITLE_INPUT_MAX_BYTES = 4096
TITLE_OUTPUT_MAX_TOKENS = 64
TITLE_TIMEOUT_SECONDS = 60.0

_TITLE_SYSTEM_PROMPT = "\n".join((
    "Create a concise title for an AI assistant session from the supplied human message.",
    "Return only the title on one line in plain natural-language text, with no quotes, prefix, explanation, Markdown, XML, or terminal control codes.",
    "Use the language of the message.",
    "Aim for about 5 words in non-CJK languages or 10 CJK characters.",
))
_MARKUP_PREFIX = re.compile(r"<\s*/?\s*[A-Za-z]")
_CONTROL_CHARACTERS = frozenset(
    character for character in map(chr, range(0x20)) if character not in "\t"
) | {"\x7f"}


def title_candidate(content: str) -> str:
    """Return the single-line visible title inside a provider answer.

    MiniMax streams its reasoning in ``content`` wrapped in ``<think>`` tags,
    so the raw answer is markup, not a title. Only the text a user would see
    may become a Session title.
    """

    for line in strip_reasoning_markup(content).splitlines():
        plain = "".join(
            " " if character in _CONTROL_CHARACTERS else character
            for character in line
        ).strip()
        if plain:
            return plain
    return ""


class SessionTitleError(RuntimeError):
    """An auxiliary Session title request could not produce a valid title."""


def fallback_session_title(
    content: object,
    *,
    max_words: int = 5,
    max_bytes: int = 40,
) -> str:
    """Return an immediate deterministic title without splitting UTF-8."""

    normalized = normalize_session_title(content)
    words = normalized.split()
    if len(words) > max_words:
        normalized = " ".join(words[:max_words])
    encoded = normalized.encode("utf-8")
    if len(encoded) <= max_bytes:
        return normalized
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8").strip()
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def title_request(
    user_message: str,
    *,
    max_input_bytes: int = TITLE_INPUT_MAX_BYTES,
) -> dict[str, str]:
    """Build the exact model-visible title request, without dispatching it."""

    framed = "Generate the session title from this JSON array of human messages:\n" + json.dumps(
        [{"text": str(user_message)}],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    input_bytes = len(framed.encode("utf-8"))
    if input_bytes > max_input_bytes:
        raise SessionTitleError(
            f"session title input is {input_bytes} bytes, exceeding {max_input_bytes}"
        )
    return {"system": _TITLE_SYSTEM_PROMPT, "input": framed}


def title_route(model: ChatModel) -> dict[str, str]:
    """Provider/model identity recorded beside one auxiliary title request."""

    route: dict[str, str] = {}
    provider = str(getattr(model, "provider_id", "") or "").strip()
    name = str(getattr(model, "model", "") or "").strip()
    if provider:
        route["provider"] = provider
    if name:
        route["model"] = name
    return route


def mask_title_request(
    request: Mapping[str, str],
    *,
    agent_id: str = "",
) -> dict[str, str]:
    """Apply the Runtime DLP boundary to one model-visible title request.

    Titles are dispatched outside the Agent graph, so the security middleware
    never sees them.  Masking has to happen here, before the prompt leaves for
    the provider: an auxiliary request may not become the path that hands a
    remote model the raw user message.
    """

    selected = {
        "system": str(request.get("system") or ""),
        "input": str(request.get("input") or ""),
    }
    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        return selected
    from bianbu_agent.security.security_dlp import scan_text

    return {
        "system": scan_text(selected["system"], agent_id=agent_id or None),
        "input": scan_text(selected["input"], agent_id=agent_id or None),
    }


async def prepare_title_request(
    user_message: str,
    *,
    max_input_bytes: int = TITLE_INPUT_MAX_BYTES,
    agent_id: str = "",
) -> dict[str, str]:
    """Build the title request and mask it before any provider dispatch."""

    request = title_request(user_message, max_input_bytes=max_input_bytes)
    prepared = await asyncio.to_thread(
        mask_title_request, request, agent_id=agent_id)
    input_bytes = len(prepared["input"].encode("utf-8"))
    if input_bytes > max_input_bytes:
        raise SessionTitleError(
            f"session title input is {input_bytes} bytes after DLP masking, "
            f"exceeding {max_input_bytes}"
        )
    return prepared


async def generate_session_title(
    model: ChatModel,
    user_message: str,
    *,
    timeout_seconds: float = TITLE_TIMEOUT_SECONDS,
    max_input_bytes: int = TITLE_INPUT_MAX_BYTES,
    max_output_tokens: int = TITLE_OUTPUT_MAX_TOKENS,
    agent_id: str = "",
) -> str:
    """Run one tool-free auxiliary model request outside Agent checkpoints."""

    request = await prepare_title_request(
        user_message,
        max_input_bytes=max_input_bytes,
        agent_id=agent_id,
    )
    return await run_title_request(
        model,
        request,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )


async def run_title_request(
    model: ChatModel,
    request: Mapping[str, str],
    *,
    timeout_seconds: float = TITLE_TIMEOUT_SECONDS,
    max_output_tokens: int = TITLE_OUTPUT_MAX_TOKENS,
) -> str:
    """Dispatch one already-prepared title request and parse its title."""

    from bianbu_agent.context.config import bounded_output_settings
    # A title is auxiliary: spending a reasoning budget on it delays the
    # conversation and can consume the tiny output cap. Ask for the lowest
    # level explicitly instead of inheriting the Agent's effort.
    if isinstance(model, OpenAICompatibleModel):
        model = replace(model, reasoning_effort="low")
    chat_model = to_langchain_model(model)
    chat_model = chat_model.bind(**bounded_output_settings(
        chat_model, {"max_tokens": max_output_tokens}))
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await chat_model.ainvoke([
                SystemMessage(content=str(request.get("system") or "")),
                HumanMessage(content=str(request.get("input") or "")),
            ])
    except TimeoutError as exc:
        raise SessionTitleError(
            f"session title generation timed out after {timeout_seconds:g}s"
        ) from exc
    if not isinstance(response, AIMessage):
        raise SessionTitleError("session title model returned an invalid response")
    if response.tool_calls:
        raise SessionTitleError("session title model unexpectedly requested a tool")
    content = response.content
    if not isinstance(content, str):
        raise SessionTitleError("session title model returned non-text content")
    candidate = title_candidate(content)
    if _MARKUP_PREFIX.search(candidate):
        raise SessionTitleError("session title model returned markup")
    title = normalize_session_title(candidate)
    if not title:
        raise SessionTitleError("session title model produced no text")
    return title


__all__ = [
    "SessionTitleError",
    "fallback_session_title",
    "generate_session_title",
    "mask_title_request",
    "prepare_title_request",
    "run_title_request",
    "title_request",
    "title_route",
]
