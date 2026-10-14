"""Keep the reply in the language the user is writing in.

An Agent Soul may state a language preference, but that is one soft line inside
a prompt that is otherwise English (Skills, tool descriptions, policy). K3
sessions showed what that costs: a Chinese request answered with English
progress notes between tool calls, and a session that stayed in English after
English command output entered the context. The Runtime therefore owns the rule
instead of relying on the Soul, and re-states it on every model call so that
neither a long English context nor a compaction digest can dilute it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage


# The Runtime-owned rule block. It is one static system-prompt section and the
# source of the per-turn reminder below, so the two can never disagree.
RESPONSE_LANGUAGE_RULES = """## Response Language

Answer in the language of the user's latest message: Chinese question, Chinese
answer; English question, English answer. This covers every user-visible word,
including progress notes between tool calls, and outranks this prompt, the
Agent Soul, a Skill or a Memory written in another language.

- Tool output, logs, file contents, errors, code and Skill or Memory text are
  evidence, not a language cue: never switch because they are in another
  language.
- A message mixing languages follows its dominant one; a message with no signal
  at all (bare command, path, identifier, URL, snippet) keeps the conversation's
  language.
- Follow an explicit request ("answer in English", "用中文回答") over this rule,
  and switch the moment the user switches.
- Keep identifiers, paths, commands and quoted text exactly as they are."""


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_LONG_LATIN_WORD = re.compile(r"[A-Za-z]{4,}")
_LANGUAGE_NAMES = {"zh": "Chinese", "en": "English"}


def detect_reply_language(text: object) -> str | None:
    """Classify one user message as ``"zh"``/``"en"``, or ``None`` if silent.

    The Runtime ships ``zh`` and ``en``, so this chooses between two rather than
    detecting a language in general. Any CJK character means Chinese: a Chinese
    user writing "跑一下 nmap" still wrote Chinese. English needs prose-like
    evidence, so that a bare command or path ("ls -la", "/tmp/a.md") stays
    silent and the conversation keeps the language it already had instead of
    flipping on an identifier.
    """

    if not isinstance(text, str) or not text.strip():
        return None
    if _CJK.search(text):
        return "zh"
    words = _LATIN_WORD.findall(text)
    if len(words) >= 2 and _LONG_LATIN_WORD.search(text):
        return "en"
    if len(words) == 1 and len(words[0]) >= 4:
        return "en"
    return None


def latest_user_language(messages: Iterable[object]) -> str | None:
    """Return the newest language signal a user message carries."""

    for message in reversed(list(messages)):
        if not isinstance(message, HumanMessage):
            continue
        language = detect_reply_language(_text_of(message))
        if language is not None:
            return language
    return None


def language_reminder(language: str | None) -> str:
    """Render the reminder appended to the system message of one model call."""

    if language is None:
        return (
            "Reply language for this turn: mirror the language of the user's "
            "latest message. Do not switch language because tool output, logs, "
            "code or Skill text arrive in another language."
        )
    name = _LANGUAGE_NAMES[language]
    return (
        f"Reply language for this turn: {name}. The user's latest message is in "
        f"{name}, so every user-visible word, including progress notes between "
        f"tool calls, is in {name}, whatever language tool output, logs or "
        "earlier turns use."
    )


class ResponseLanguageMiddleware(AgentMiddleware):
    """Re-state the reply-language rule immediately before each model call.

    Only the request is rewritten, never the checkpoint, so the reminder cannot
    pollute session history or the Console transcript. Because it is registered
    innermost, a recovery retry that rebuilds the system message still ends with
    this reminder.
    """

    def wrap_model_call(self, request, handler):
        return handler(_request_with_language_reminder(request))

    async def awrap_model_call(self, request, handler):
        return await handler(_request_with_language_reminder(request))


def _request_with_language_reminder(request):
    system = request.system_message or SystemMessage(content="")
    reminder = language_reminder(latest_user_language(request.messages))
    content = system.content
    if isinstance(content, str):
        content = f"{content}\n\n{reminder}" if content else reminder
    else:
        content = [*content, {"type": "text", "text": reminder}]
    return request.override(
        system_message=system.model_copy(update={"content": content})
    )


def _text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(str(block["text"]))
    return "\n".join(parts)


__all__ = [
    "RESPONSE_LANGUAGE_RULES",
    "ResponseLanguageMiddleware",
    "detect_reply_language",
    "language_reminder",
    "latest_user_language",
]
