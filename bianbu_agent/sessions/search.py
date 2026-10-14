"""Case-insensitive substring search over folded Session conversation text.

The read model is the same fold ``GET /sessions/{id}/history`` serves, so a hit
always points at a message the conversation view can display: ``seq`` is the
folded message anchor the client scrolls to.  Text that the display fold keeps
out of the conversation (reasoning, tool traffic, hidden console operations) is
not searched, because a hit the user cannot open is worse than no hit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# 命中片段两侧各保留的字符数：够看清上下文，又不把整条正文回给界面。
SNIPPET_PADDING = 40

# 会话正文的两个来源：用户输入与助手回答。其余折叠事件不参与检索。
_TEXT_FIELDS: Mapping[str, tuple[str, str]] = {
    "user/message": ("user", "content"),
    "assistant/text.delta": ("assistant", "text"),
}


def visible_text(event: Mapping[str, object]) -> tuple[str, str]:
    """Return ``(role, text)`` for a folded event that carries conversation text."""

    selected = _TEXT_FIELDS.get(str(event.get("type", "")))
    if selected is None:
        return "", ""
    role, key = selected
    data = event.get("data")
    value = data.get(key) if isinstance(data, Mapping) else None
    return role, value if isinstance(value, str) else ""


def snippet_for(text: str, needle: str) -> tuple[str, int, int] | None:
    """Locate ``needle`` case-insensitively and return ``(snippet, offset, length)``.

    ``casefold`` is the matching form: it folds Latin case the way a user expects
    and leaves CJK alone.  The offset is reported back for highlighting in the
    snippet; a fold that changes length (``ß`` -> ``ss``) can shift it by a
    character, which only shortens the highlight, never the match.
    """

    start = text.casefold().find(needle)
    if start < 0 or not needle:
        return None
    begin = max(0, start - SNIPPET_PADDING)
    end = min(len(text), start + len(needle) + SNIPPET_PADDING)
    prefix = "…" if begin else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[begin:end] + suffix, start - begin + len(prefix), len(needle)


def search_folded(
    folded: Sequence[Mapping[str, object]],
    needle: str,
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return newest-first hits of one Session, at most ``limit`` of them.

    ``seq`` is the fold anchor and ``end_seq`` the last event that anchor
    covers.  A reader pages ``/history?before_seq=end_seq+1`` to get the whole
    message: cutting the window at ``seq`` would show only the first streamed
    chunk of an answer.
    """

    hits: list[dict[str, object]] = []
    for event in reversed(folded):
        role, text = visible_text(event)
        if not text:
            continue
        found = snippet_for(text, needle)
        if found is None:
            continue
        snippet, offset, length = found
        seq = int(event.get("seq", 0))
        data = event.get("data")
        span = data.get("seq_range") if isinstance(data, Mapping) else None
        end_seq = (
            int(span[1])
            if isinstance(span, (list, tuple))
            and len(span) == 2
            and isinstance(span[1], int)
            else seq
        )
        run_id = event.get("run_id")
        hits.append(
            {
                "seq": seq,
                "end_seq": max(end_seq, seq),
                "run_id": None if run_id is None else str(run_id),
                "role": role,
                "snippet": snippet,
                "match_start": offset,
                "match_length": length,
            }
        )
        if len(hits) >= limit:
            break
    return hits


__all__ = ["SNIPPET_PADDING", "search_folded", "snippet_for", "visible_text"]
