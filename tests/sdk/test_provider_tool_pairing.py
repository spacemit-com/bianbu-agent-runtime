"""Provider payloads must pair every tool call with a tool result."""

from __future__ import annotations

from bianbu_agent.engines.tool_pairing import (
    INVALID_ARGUMENTS_RESULT,
    UNFINISHED_TOOL_RESULT,
    reconcile_tool_pairs,
)


def _assistant(*calls: tuple[str, str]) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
            for call_id, name in calls
        ],
    }


def _tool(call_id: str, content: str = "done") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_invalid_tool_call_is_answered_with_its_own_reason() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        _assistant(("call-ok", "list_apps"), ("call-bad", "get_system_info")),
        _tool("call-ok"),
    ]

    paired = reconcile_tool_pairs(messages, invalid_call_ids={"call-bad"})

    assert [message["role"] for message in paired] == [
        "user", "assistant", "tool", "tool",
    ]
    assert paired[2]["tool_call_id"] == "call-ok"
    assert paired[3]["tool_call_id"] == "call-bad"
    assert paired[3]["content"] == INVALID_ARGUMENTS_RESULT
    assert paired[3]["name"] == "get_system_info"


def test_unanswered_tool_call_is_closed_before_the_next_turn() -> None:
    messages = [
        _assistant(("call-1", "terminal")),
        {"role": "user", "content": "next"},
    ]

    paired = reconcile_tool_pairs(messages)

    assert [message["role"] for message in paired] == ["assistant", "tool", "user"]
    assert paired[1]["tool_call_id"] == "call-1"
    assert paired[1]["content"] == UNFINISHED_TOOL_RESULT


def test_trailing_unanswered_tool_call_is_closed_at_the_end() -> None:
    paired = reconcile_tool_pairs([_assistant(("call-1", "terminal"))])

    assert [message["role"] for message in paired] == ["assistant", "tool"]


def test_orphan_tool_result_is_dropped() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        _assistant(("call-1", "terminal")),
        _tool("call-1"),
        _tool("call-orphan"),
    ]

    paired = reconcile_tool_pairs(messages)

    assert [message["tool_call_id"] for message in paired if "tool_call_id" in message] == [
        "call-1"
    ]


def test_complete_batch_is_untouched() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        _assistant(("call-1", "terminal"), ("call-2", "todo")),
        _tool("call-1"),
        _tool("call-2"),
        {"role": "assistant", "content": "done"},
    ]

    assert reconcile_tool_pairs(messages) == messages


def test_call_without_id_is_left_alone() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "terminal", "arguments": "{}"}}],
        },
    ]

    paired = reconcile_tool_pairs(messages)

    assert paired == messages
