"""Regression checks for intentionally supported package-level imports."""

import bianbu_agent.runs as runs
import pytest

from bianbu_agent import RunHandle


def test_runs_does_not_export_removed_followup_contracts() -> None:
    assert not {"FollowupBatch", "FollowupItem", "FollowupState"} & set(runs.__all__)
    assert not hasattr(runs, "FollowupBatch")
    assert not hasattr(runs, "FollowupItem")
    assert not hasattr(runs, "SteeringInstruction")


def test_run_control_module_does_not_export_legacy_steering_instruction() -> None:
    """The durable queue has no in-memory steering instruction contract."""

    with pytest.raises(ImportError):
        from bianbu_agent.runs.control import SteeringInstruction


def test_run_handle_exposes_no_removed_input_controls() -> None:
    assert not hasattr(RunHandle, "enqueue_followup")
    assert not hasattr(RunHandle, "list_followups")
    assert not hasattr(RunHandle, "interrupt_for_followup")
    assert not hasattr(RunHandle, "steer")
