from __future__ import annotations

import json
from pathlib import Path

import pytest

from bianbu_agent.security.runtime_process.protocol import ProtocolError, decode_message, encode_message, request, response
from bianbu_agent.security.runtime_process.worker_rpc import make_instance_socket


def test_protocol_rejects_unknown_version_and_type() -> None:
    with pytest.raises(ProtocolError):
        encode_message({**request("health", "r1", "i1"), "protocol": 99})
    with pytest.raises(ProtocolError):
        encode_message(request("exec.shell", "r1", "i1"))


def test_protocol_requires_event_sequence_and_identity() -> None:
    message = response("run.event", "r1", "i1", run_id="run", session_id="session", sequence=1, event={"kind": "token", "payload": {}})
    assert decode_message(encode_message(message, direction="response"), direction="response")["sequence"] == 1
    with pytest.raises(ProtocolError):
        encode_message({**message, "sequence": 0}, direction="response")


def test_protocol_rejects_unknown_fields_and_invalid_run_start() -> None:
    with pytest.raises(ProtocolError, match="unknown fields"):
        encode_message({**request("health", "r1", "i1"), "command": ["/bin/sh"]})
    with pytest.raises(ProtocolError, match="session_id"):
        encode_message(request("run.start", "r1", "i1", token="test-token",
                               run_id="run", prompt="hello"))
    with pytest.raises(ProtocolError, match="thread_id"):
        encode_message(request(
            "run.start", "r1", "i1", run_id="run", session_id="session",
            prompt="hello", token="test-token",
        ))
    with pytest.raises(ProtocolError, match="account_id"):
        encode_message(request(
            "run.start", "r1", "i1", run_id="run", session_id="session",
            thread_id="thread", prompt="hello", token="test-token",
        ))

    message = request(
        "run.start", "r1", "i1", run_id="run", session_id="session",
        thread_id="thread", prompt="hello", account_id="system-user-42",
        token="test-token",
    )
    assert decode_message(encode_message(message))["thread_id"] == "thread"
    assert decode_message(encode_message(message))["account_id"] == "system-user-42"


def test_protocol_accepts_log_only_title_request() -> None:
    """A title crosses the same strict contract as every other Worker call."""

    message = request(
        "title.generate", "r1", "i1", prompt="检查 K3 服务",
        agent_id="com.bianbu.demo", token="test-token",
    )
    assert decode_message(encode_message(message))["prompt"] == "检查 K3 服务"
    with pytest.raises(ProtocolError, match="agent_id"):
        encode_message(request(
            "title.generate", "r2", "i1", prompt="检查 K3 服务", token="test-token",
        ))
    with pytest.raises(ProtocolError, match="unknown fields"):
        encode_message({
            **message, "run_id": "run",
        })

    reply = response(
        "title.generate.result", "r1", "i1", title="K3 服务检查",
        agent_id="com.bianbu.demo",
        request={"system": "instructions", "input": "title input"},
        route={"provider": "custom:bibit", "model": "deepseek-flash"},
    )
    decoded = decode_message(encode_message(reply, direction="response"),
                             direction="response")
    assert decoded["title"] == "K3 服务检查"
    assert decoded["route"]["provider"] == "custom:bibit"
    with pytest.raises(ProtocolError, match="unknown fields"):
        encode_message({**reply, "run_id": "run"}, direction="response")


def test_protocol_validates_structured_session_input() -> None:
    base = request(
        "run.start", "r1", "i1", run_id="run", session_id="session",
        thread_id="thread", prompt="hello", account_id="system-user-42",
        token="test-token",
    )
    with pytest.raises(ProtocolError, match="input message"):
        encode_message({
            **base,
            "input_messages": [{
                "message_id": "message-1",
                "mode": "queue",
                "content": "hello",
                "attachments": "not-an-array",
                "created_at": 1,
            }],
        })

    with pytest.raises(ProtocolError, match="invalid step input"):
        encode_message(request(
            "run.step_input", "r2", "i1", run_id="run", token="test-token",
            messages=[{
                "message_id": "message-2",
                "mode": "queue",
                "content": "later",
                "attachments": [],
                "created_at": 2,
            }],
        ))


def test_worker_socket_path_is_private_and_rejects_traversal(tmp_path: Path) -> None:
    path = make_instance_socket(tmp_path, "instance-1")
    assert path == tmp_path / "instance-1" / "worker.sock"
    with pytest.raises(ValueError):
        make_instance_socket(tmp_path, "../escape")


def test_worker_socket_path_shortens_long_instance_ids(tmp_path: Path) -> None:
    instance = "com.bianbu.agent.hostacceptance-" + "a" * 64
    path = make_instance_socket(tmp_path, instance)
    assert path.parent == tmp_path.resolve()
    assert path.name.startswith("worker-")
    assert path.name.endswith(".sock")
    assert len(str(path).encode()) <= 100
    assert make_instance_socket(tmp_path, instance) == path
    other = make_instance_socket(tmp_path, instance + "b")
    assert other != path
