from __future__ import annotations


DEFAULT_TOOL_RESULT_LIMIT = 16_384
_MINIMUM_LIMIT = 128


def compact_tool_result(
    content: str,
    *,
    limit: int = DEFAULT_TOOL_RESULT_LIMIT,
) -> str:
    """Bound model-visible tool text while retaining useful head and tail data."""

    text = str(content)
    if isinstance(limit, bool) or limit < _MINIMUM_LIMIT:
        raise ValueError(f"tool result limit must be at least {_MINIMUM_LIMIT}")
    if len(text) <= limit:
        return text

    omitted = len(text) - limit
    while True:
        marker = (
            "\n...[tool result truncated: "
            f"{omitted:,} characters omitted]...\n"
        )
        retained = max(0, limit - len(marker))
        revised = len(text) - retained
        if revised == omitted:
            break
        omitted = revised
    head = (retained + 1) // 2
    tail = retained - head
    return text[:head] + marker + (text[-tail:] if tail else "")


__all__ = ["DEFAULT_TOOL_RESULT_LIMIT", "compact_tool_result"]
