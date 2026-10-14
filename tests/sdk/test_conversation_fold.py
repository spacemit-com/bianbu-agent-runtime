"""The transcript fold is the whole conversation state without a checkpointer."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bianbu_agent.engines.langgraph.conversation import fold_messages
from bianbu_agent.sessions.events import SessionEvent


def _event(seq: int, event_type: str, data: dict, *, run_id: str = "run-1"):
    return SessionEvent(
        session_id="session-a",
        seq=seq,
        type=event_type,
        time=float(seq),
        run_id=run_id,
        data=data,
    )


def _input_id(run_id: str) -> str:
    return f"bianbu-agent-run:{run_id}"


def test_text_turns_fold_into_a_human_and_an_assistant_message() -> None:
    folded = fold_messages(
        [
            _event(0, "user/message", {"content": "hi"}),
            _event(1, "assistant/text.delta", {"text": "he"}),
            _event(2, "assistant/text.delta", {"text": "llo"}),
            _event(3, "assistant/message.end", {"has_tool_calls": False}),
        ],
        input_id_for_run=_input_id,
    )

    assert [type(message) for message in folded] == [HumanMessage, AIMessage]
    assert folded[0].content == "hi"
    assert folded[1].content == "hello"


def test_a_run_started_without_an_inbox_carries_the_run_derived_id() -> None:
    folded = fold_messages(
        [_event(0, "user/message", {"content": "hi"})],
        input_id_for_run=_input_id,
    )

    assert folded[0].id == "bianbu-agent-run:run-1"


def test_an_inbox_user_message_keeps_its_own_id() -> None:
    folded = fold_messages(
        [_event(0, "user/message", {"content": "hi", "message_id": "m-7"})],
        input_id_for_run=_input_id,
    )

    assert folded[0].id == "m-7"


def test_attachments_ride_along_in_additional_kwargs() -> None:
    folded = fold_messages(
        [
            _event(
                0,
                "user/message",
                {
                    "content": "look",
                    "message_id": "m-1",
                    "attachments": [{"file_path": "attachments/a.png"}],
                },
            )
        ],
        input_id_for_run=_input_id,
    )

    assert folded[0].additional_kwargs["attachments"] == [
        {"file_path": "attachments/a.png"}
    ]


def test_tool_calls_and_results_keep_their_original_ids_and_bytes() -> None:
    folded = fold_messages(
        [
            _event(0, "user/message", {"content": "read it", "message_id": "m-1"}),
            _event(1, "assistant/text.delta", {"text": "on it"}),
            _event(2, "assistant/message.end", {"has_tool_calls": True}),
            _event(
                3,
                "tool/call.started",
                {"call_id": "c-1", "name": "read_file", "arguments": {"path": "a"}},
            ),
            _event(
                4,
                "tool/call.completed",
                {"call_id": "c-1", "name": "read_file", "result": "body"},
            ),
            _event(5, "assistant/text.delta", {"text": "done"}),
            _event(6, "assistant/message.end", {"has_tool_calls": False}),
        ],
        input_id_for_run=_input_id,
    )

    assert [type(message) for message in folded] == [
        HumanMessage, AIMessage, ToolMessage, AIMessage,
    ]
    assert folded[1].content == "on it"
    assert folded[1].tool_calls == [
        {"name": "read_file", "args": {"path": "a"}, "id": "c-1", "type": "tool_call"}
    ]
    assert folded[2].content == "body"
    assert folded[2].tool_call_id == "c-1"
    assert folded[3].content == "done"


def test_a_tool_call_that_never_finished_is_closed_with_an_error() -> None:
    """A cancelled Run must not leave a provider-invalid dangling tool call."""

    folded = fold_messages(
        [
            _event(0, "user/message", {"content": "go", "message_id": "m-1"}),
            _event(1, "assistant/message.end", {"has_tool_calls": True}),
            _event(
                2,
                "tool/call.started",
                {"call_id": "c-1", "name": "terminal", "arguments": {}},
            ),
        ],
        input_id_for_run=_input_id,
    )

    assert isinstance(folded[1], AIMessage)
    assert folded[1].tool_calls[0]["id"] == "c-1"
    assert isinstance(folded[2], ToolMessage)
    assert folded[2].tool_call_id == "c-1"
    assert folded[2].status == "error"
    assert "not executed" in folded[2].content


def test_a_rejected_tool_call_is_closed_immediately() -> None:
    folded = fold_messages(
        [
            _event(0, "user/message", {"content": "go", "message_id": "m-1"}),
            _event(1, "assistant/message.end", {"has_tool_calls": True}),
            _event(
                2,
                "tool/call.rejected",
                {"call_id": "c-9", "name": "terminal", "arguments": {}},
            ),
        ],
        input_id_for_run=_input_id,
    )

    assert isinstance(folded[2], ToolMessage)
    assert folded[2].tool_call_id == "c-9"
    assert folded[2].status == "error"


def test_steer_input_lands_where_it_was_applied() -> None:
    folded = fold_messages(
        [
            _event(0, "user/message", {"content": "one", "message_id": "m-1"}),
            _event(1, "assistant/text.delta", {"text": "first"}),
            _event(2, "assistant/message.end", {"has_tool_calls": False}),
            _event(3, "agent/inbox/claimed", {"message_ids": ["m-2"]}),
            _event(4, "user/message", {"content": "two", "message_id": "m-2"}),
        ],
        input_id_for_run=_input_id,
    )

    assert [type(message) for message in folded] == [
        HumanMessage, AIMessage, HumanMessage,
    ]
    assert folded[2].content == "two"


def test_bookkeeping_events_never_become_messages() -> None:
    folded = fold_messages(
        [
            _event(0, "request/context", {"system_prompt": "s"}),
            _event(1, "user/message", {"content": "hi", "message_id": "m-1"}),
            _event(2, "hook/invoked", {"point": "before_model"}),
            _event(3, "run/progress", {"phase": "thinking"}),
            _event(4, "assistant/reasoning.delta", {"text": "hmm"}),
            _event(5, "context/compressed", {"compressed": True}),
            _event(6, "run/completed", {"status": "completed"}),
        ],
        input_id_for_run=_input_id,
    )

    assert [type(message) for message in folded] == [HumanMessage]


def test_an_empty_transcript_folds_to_nothing() -> None:
    assert fold_messages([], input_id_for_run=_input_id) == []
