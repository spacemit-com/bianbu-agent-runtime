from __future__ import annotations

from bianbu_agent.context.tool_results import compact_tool_result


def test_large_tool_text_keeps_bounded_head_tail_and_exact_omission_count() -> None:
    content = "HEAD" + ("x" * 20_000) + "TAIL"

    compacted = compact_tool_result(content, limit=1_000)

    assert len(compacted) <= 1_000
    assert compacted.startswith("HEAD")
    assert compacted.endswith("TAIL")
    assert "[tool result truncated: 19,066 characters omitted]" in compacted


def test_small_tool_text_is_unchanged() -> None:
    assert compact_tool_result("small result", limit=1_000) == "small result"
