from __future__ import annotations

import pytest

from bianbu_agent import RunControl
from bianbu_agent.server.runtime import BianbuAgentRuntime
from bianbu_agent.security.runtime_process.protocol import (
    ProtocolError,
    validate_message,
)


def test_session_terminate_requires_reason():
    with pytest.raises(ProtocolError):
        validate_message({
            "protocol": 1,
            "type": "session.terminate",
            "request_id": "r",
            "instance_id": "i",
            "token": "t",
            "session_id": "s",
            "agent_id": "a",
            "reason": "",
        })


def test_session_terminate_accepts_explicit_reason():
    message = validate_message({
        "protocol": 1,
        "type": "session.terminate",
        "request_id": "r",
        "instance_id": "i",
        "token": "t",
        "session_id": "s",
        "agent_id": "a",
        "reason": "session_deleted",
    })
    assert message["reason"] == "session_deleted"


def test_session_terminate_result_requires_boolean():
    with pytest.raises(ProtocolError):
        validate_message({
            "protocol": 1,
            "type": "session.terminate.result",
            "request_id": "r",
            "instance_id": "i",
            "session_id": "s",
            "cleared": "yes",
        }, direction="response")


def test_stand_in_state_stays_private_to_run_control():
    control = RunControl(request_id="run", session_id="session")
    control.dlp_stand_ins["13879455773"] = "13812345678"
    assert "13879455773" not in repr(control)
    assert "13812345678" not in repr(control)


@pytest.mark.asyncio
async def test_runtime_terminate_session_clears_local_references(monkeypatch):
    from bianbu_agent.security import security_dlp

    security_dlp._REFERENCES.get_or_create(
        "session-terminate", "phone", "13812345678",
    )
    assert security_dlp._REFERENCES.mapping("session-terminate")

    runtime = object.__new__(BianbuAgentRuntime)

    class Plugins:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lease(self):
            yield object()

    runtime._plugin_manager = Plugins()
    runtime.__dict__["_control_plugin_runtime"] = None
    assert await runtime.terminate_session(
        "session-terminate",
        agent_id="agent",
        reason="session_deleted",
    )
    assert security_dlp._REFERENCES.mapping("session-terminate") == {}
