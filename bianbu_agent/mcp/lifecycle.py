"""One vocabulary for what an MCP server is doing right now.

The Runtime already reports a status from several layers: the preparation
manager says ``preparing``/``ready``/``failed``, a directory Plugin reports
``healthy``/``degraded``/``unhealthy``, and the Control catalog says
``not_loaded`` until a Worker has something to say.  Every consumer had to
guess what a raw value meant: ``not_loaded`` could be "no Expert selects this
server" or "the Worker never loaded it", and ``preparing`` never said what it
was doing.  Adding another raw status would only move the guessing around.

This module projects all of them onto one lifecycle, plus the phase that
explains an in-flight preparation, so the Console, the BiBit Runtime plugin and
the AgentHub view can all say the same thing about the same server.
"""

from __future__ import annotations

#: No Worker has loaded this server yet (declared, or never selected).
MCP_STATE_NOT_LOADED = "not_loaded"
#: Dependencies are being installed and/or the server is being started.
MCP_STATE_PREPARING = "preparing"
#: The server answered a handshake; its tools are known and callable.
MCP_STATE_READY = "ready"
#: Loaded, but something the server needs is missing (tools unavailable).
MCP_STATE_DEGRADED = "degraded"
#: Loading or running failed; ``detail`` carries the reason.
MCP_STATE_FAILED = "failed"
#: Deliberately not running (unloaded, disabled, or shut down).
MCP_STATE_STOPPED = "stopped"
#: Nothing could be read, e.g. the Worker catalog is unavailable.
MCP_STATE_UNKNOWN = "unknown"

MCP_STATES = (
    MCP_STATE_NOT_LOADED,
    MCP_STATE_PREPARING,
    MCP_STATE_READY,
    MCP_STATE_DEGRADED,
    MCP_STATE_FAILED,
    MCP_STATE_STOPPED,
    MCP_STATE_UNKNOWN,
)

#: The npm package is being vendored into the data directory.
MCP_PHASE_INSTALLING = "installing"
#: The local installation is being started and handshaken.
MCP_PHASE_STARTING = "starting"

MCP_PHASES = (MCP_PHASE_INSTALLING, MCP_PHASE_STARTING)

_STATUS_TO_STATE = {
    "healthy": MCP_STATE_READY,
    "ready": MCP_STATE_READY,
    "running": MCP_STATE_READY,
    "preparing": MCP_STATE_PREPARING,
    "starting": MCP_STATE_PREPARING,
    "degraded": MCP_STATE_DEGRADED,
    "failed": MCP_STATE_FAILED,
    "unhealthy": MCP_STATE_FAILED,
    "error": MCP_STATE_FAILED,
    "stopped": MCP_STATE_STOPPED,
    "not_loaded": MCP_STATE_NOT_LOADED,
    "unknown": MCP_STATE_UNKNOWN,
    "": MCP_STATE_UNKNOWN,
}


def mcp_lifecycle_state(status: object) -> str:
    """Project any layer's raw status onto the shared lifecycle.

    Unknown values map to ``unknown`` rather than raising: a status added by a
    newer Worker may not be known here, and a listing endpoint must stay
    readable in that case.
    """

    return _STATUS_TO_STATE.get(str(status or "").strip().lower(), MCP_STATE_UNKNOWN)


def mcp_state_ready(state: object) -> bool:
    return str(state or "").strip().lower() == MCP_STATE_READY


def mcp_phase(value: object) -> str:
    """The preparation phase, or "" when the value is not a known phase."""

    phase = str(value or "").strip().lower()
    return phase if phase in MCP_PHASES else ""


__all__ = [
    "MCP_PHASES",
    "MCP_PHASE_INSTALLING",
    "MCP_PHASE_STARTING",
    "MCP_STATES",
    "MCP_STATE_DEGRADED",
    "MCP_STATE_FAILED",
    "MCP_STATE_NOT_LOADED",
    "MCP_STATE_PREPARING",
    "MCP_STATE_READY",
    "MCP_STATE_STOPPED",
    "MCP_STATE_UNKNOWN",
    "mcp_lifecycle_state",
    "mcp_phase",
    "mcp_state_ready",
]
