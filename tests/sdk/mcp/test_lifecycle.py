from __future__ import annotations

import pytest

from bianbu_agent.mcp.lifecycle import (
    MCP_PHASE_INSTALLING,
    MCP_PHASE_STARTING,
    MCP_STATES,
    MCP_STATE_DEGRADED,
    MCP_STATE_FAILED,
    MCP_STATE_NOT_LOADED,
    MCP_STATE_PREPARING,
    MCP_STATE_READY,
    MCP_STATE_STOPPED,
    MCP_STATE_UNKNOWN,
    mcp_lifecycle_state,
    mcp_phase,
    mcp_state_ready,
)


@pytest.mark.parametrize(
    "status,expected",
    [
        # Every layer's vocabulary has to land on one lifecycle, or a UI ends
        # up rendering "preparing" and "healthy" as unrelated states.
        ("healthy", MCP_STATE_READY),
        ("ready", MCP_STATE_READY),
        ("running", MCP_STATE_READY),
        ("preparing", MCP_STATE_PREPARING),
        ("starting", MCP_STATE_PREPARING),
        ("degraded", MCP_STATE_DEGRADED),
        ("failed", MCP_STATE_FAILED),
        ("unhealthy", MCP_STATE_FAILED),
        ("error", MCP_STATE_FAILED),
        ("stopped", MCP_STATE_STOPPED),
        ("not_loaded", MCP_STATE_NOT_LOADED),
        ("unknown", MCP_STATE_UNKNOWN),
        ("", MCP_STATE_UNKNOWN),
        (None, MCP_STATE_UNKNOWN),
        # A status a newer Worker invents must not make a listing unreadable.
        ("quantum", MCP_STATE_UNKNOWN),
    ],
)
def test_every_layer_status_lands_on_the_shared_lifecycle(status, expected) -> None:
    assert mcp_lifecycle_state(status) == expected
    assert expected in MCP_STATES


def test_readiness_is_only_the_answering_state() -> None:
    assert mcp_state_ready(MCP_STATE_READY) is True
    for state in MCP_STATES:
        if state != MCP_STATE_READY:
            assert mcp_state_ready(state) is False


@pytest.mark.parametrize(
    "value,expected",
    [
        ("installing", MCP_PHASE_INSTALLING),
        ("starting", MCP_PHASE_STARTING),
        ("INSTALLING", MCP_PHASE_INSTALLING),
        ("", ""),
        (None, ""),
        ("ready", ""),
    ],
)
def test_only_known_phases_survive(value, expected) -> None:
    assert mcp_phase(value) == expected
