from __future__ import annotations

from pathlib import Path

from bianbu_agent.security.capability_declarations import (
    DeclarationBaseline,
    plan_replay,
)


def test_replay_declares_rows_securityd_never_saw():
    plan = plan_replay(declared={"terminal": "allow"}, current={}, previous={})
    assert plan.writes == {"terminal": "allow"}
    assert plan.preserved == {}
    assert not plan.empty


def test_replay_leaves_matching_rows_alone():
    plan = plan_replay(declared={"terminal": "allow"},
                       current={"terminal": "allow"}, previous={})
    assert plan.empty


def test_replay_follows_a_new_default_over_an_untouched_row():
    """The row still holds what this Runtime declared, so the upgrade wins."""
    plan = plan_replay(declared={"terminal": "allow"},
                       current={"terminal": "ask"},
                       previous={"terminal": "ask"})
    assert plan.writes == {"terminal": "allow"}


def test_replay_preserves_the_operators_edit():
    """The row no longer holds the declared value: the console owns it."""
    plan = plan_replay(declared={"terminal": "allow"},
                       current={"terminal": "deny"},
                       previous={"terminal": "ask"})
    assert plan.empty
    assert plan.preserved == {"terminal": "deny"}


def test_replay_treats_missing_history_as_stale_defaults():
    """No baseline yet: an older release's default must not survive."""
    plan = plan_replay(declared={"terminal": "allow"},
                       current={"terminal": "ask"}, previous=None)
    assert plan.writes == {"terminal": "allow"}


def test_baseline_round_trips_declared_values(tmp_path: Path):
    baseline = DeclarationBaseline.in_data_dir(tmp_path)
    assert baseline.declared_for("agent") is None

    baseline.record("agent", {"terminal": "allow", "patch": "ask"})
    assert baseline.declared_for("agent") == {"terminal": "allow", "patch": "ask"}

    baseline.record("other", {"terminal": "deny"})
    assert baseline.declared_for("agent") == {"terminal": "allow", "patch": "ask"}
    assert baseline.declared_for("other") == {"terminal": "deny"}


def test_baseline_ignores_an_unreadable_document(tmp_path: Path):
    baseline = DeclarationBaseline.in_data_dir(tmp_path)
    baseline.path.parent.mkdir(parents=True, exist_ok=True)
    baseline.path.write_text("{ not json", encoding="utf-8")
    assert baseline.load() == {}
