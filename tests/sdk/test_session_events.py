from __future__ import annotations

from types import MappingProxyType

import pytest

from bianbu_agent.runs import RunError, RunEvent, RunResult, Usage
from bianbu_agent.sessions.events import (
    SessionEvent,
    session_event_for_run_event,
    session_event_for_run_result,
    session_event_for_run_start,
)


def test_session_event_enforces_identity_and_immutable_data() -> None:
    event = SessionEvent(
        session_id="session-a",
        seq=0,
        type="user/message",
        time=10.5,
        run_id="run-a",
        data={"content": "hello"},
    )

    assert event.data == {"content": "hello"}
    assert isinstance(event.data, MappingProxyType)
    with pytest.raises(ValueError, match="session_id"):
        SessionEvent("", 0, "user/message", 0, None, {})
    with pytest.raises(ValueError, match="seq"):
        SessionEvent("s", -1, "user/message", 0, None, {})
    with pytest.raises(ValueError, match="type"):
        SessionEvent("s", 0, "", 0, None, {})


def test_run_facts_map_to_canonical_session_event_types() -> None:
    start = session_event_for_run_start(
        "session-a",
        "run-a",
        {
            "prompt": "hello",
            "thread_id": "thread-a",
            "metadata": {"attachments": [{"name": "a.txt"}]},
        },
        seq=0,
        event_time=1.0,
    )
    reasoning = session_event_for_run_event(
        "session-a", "run-a", RunEvent("reasoning", {"text": "think"}),
        seq=1, event_time=2.0,
    )
    token = session_event_for_run_event(
        "session-a", "run-a", RunEvent("token", {"text": "answer"}),
        seq=2, event_time=3.0,
    )
    tool = session_event_for_run_event(
        "session-a", "run-a",
        RunEvent("tool_start", {"call_id": "c1", "name": "search", "arguments": {"q": "x"}}),
        seq=3, event_time=4.0,
    )
    unknown = session_event_for_run_event(
        "session-a", "run-a", RunEvent("artifact", {"path": "out.txt"}),
        seq=4, event_time=5.0,
    )

    assert start.type == "user/message"
    assert start.data == {
        "content": "hello",
        "attachments": [{"name": "a.txt"}],
        "thread_id": "thread-a",
        "request": {
            "prompt": "hello",
            "thread_id": "thread-a",
            "metadata": {"attachments": [{"name": "a.txt"}]},
        },
    }
    assert reasoning.type == "assistant/reasoning.delta"
    assert token.type == "assistant/text.delta"
    assert tool.type == "tool/call.started"
    assert unknown.type == "run/event"
    assert unknown.data == {"kind": "artifact", "payload": {"path": "out.txt"}}


def test_terminal_results_map_status_usage_and_error() -> None:
    completed = session_event_for_run_result(
        "session-a",
        RunResult(
            request_id="run-a",
            status="completed",
            content="answer",
            usage=Usage(input_tokens=2, output_tokens=3, total_tokens=5),
        ),
        seq=3,
        event_time=4.0,
    )
    failed = session_event_for_run_result(
        "session-a",
        RunResult(
            request_id="run-b",
            status="failed",
            error=RunError("provider_error", "offline", {"attempts": 3},
                           retryable=False),
        ),
        seq=4,
        event_time=5.0,
    )

    assert completed.type == "run/completed"
    assert completed.data["content"] == "answer"
    assert completed.data["usage"] == {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
        "metadata": {},
    }
    assert failed.type == "run/failed"
    assert failed.data["error"] == {
        "code": "provider_error",
        "message": "offline",
        "retryable": False,
        "details": {"attempts": 3},
    }
    # 终态快照与 error 字段必须一致：客户端只读一个来源。
    assert failed.data["result"]["error"] == failed.data["error"]


def test_terminal_error_message_is_never_empty() -> None:
    """无消息异常的 str() 是空串；终态事件不能只留下一个看不懂的 code。"""
    failed = session_event_for_run_result(
        "session-a",
        RunResult(request_id="run-c", status="failed",
                  error=RunError("run_failed", "")),
        seq=5,
        event_time=6.0,
    )

    assert failed.data["error"]["message"] == "run_failed"
