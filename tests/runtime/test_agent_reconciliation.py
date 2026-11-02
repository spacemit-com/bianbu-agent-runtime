"""Reconcile only orphaned securityd Agent identities, never every Agent."""
from __future__ import annotations

import pytest

from bianbu_agent.security.agent_reconciliation import (
    apply_reconciliation,
    plan_reconciliation,
)


def test_plan_removes_only_orphans_and_protects_services_and_busy_agents():
    plan = plan_reconciliation(
        [
            {"agent_id": "com.bianbu.5dc85ba6288c"},
            {"agent_id": "com.bianbu.stale"},
            {"agent_id": "com.bianbu.runtime"},
            {"agent_id": "com.bianbu.other"},
            {"agent_id": "com.bianbu.busy"},
        ],
        ["com.bianbu.5dc85ba6288c"],
        keep=["com.bianbu.other"],
        active=["com.bianbu.busy"],
        is_service=lambda agent_id: agent_id == "com.bianbu.runtime",
    )
    assert plan.remove == ("com.bianbu.stale",)
    assert plan.keep == ("com.bianbu.5dc85ba6288c", "com.bianbu.other")
    assert plan.service == ("com.bianbu.runtime",)
    assert plan.active == ("com.bianbu.busy",)


def test_plan_never_touches_another_products_agents():
    """Hermes and DSH share securityd; Runtime reconciliation must not delete them."""

    plan = plan_reconciliation(
        ["com.hermes.default", "com.dsh.agent"],
        [],
    )
    assert plan.remove == ()
    assert plan.keep == ("com.dsh.agent", "com.hermes.default")


def test_a_stale_runtime_agent_is_inside_the_managed_namespace():
    plan = plan_reconciliation(["com.bianbu.stale"], [])
    assert plan.remove == ("com.bianbu.stale",)


def test_a_permanent_service_identity_is_never_offered_for_removal():
    """The service principal is not a user Expert, so it must never be an orphan."""

    plan = plan_reconciliation(
        ["com.bianbu.runtime", "com.bianbu.runtime.development", "com.bianbu.agent"],
        [],
        is_service=lambda agent_id: agent_id.startswith("com.bianbu."),
    )
    assert plan.remove == ()
    assert plan.service == (
        "com.bianbu.agent", "com.bianbu.runtime", "com.bianbu.runtime.development",
    )


def test_dry_run_reports_the_plan_without_calling_securityd():
    calls = []
    plan = plan_reconciliation(["com.bianbu.stale"], [])
    result = apply_reconciliation(plan, calls.append, dry_run=True)
    assert calls == []
    assert result.removed == []
    assert result.as_dict()["would_remove"] == ["com.bianbu.stale"]
    assert result.as_dict()["dry_run"] is True


def test_a_failed_unregister_stops_instead_of_leaving_a_partial_silent_result():
    def unregister(agent_id):
        if agent_id == "com.bianbu.bad":
            raise RuntimeError("securityd unavailable")
        return True

    plan = plan_reconciliation(["com.bianbu.a", "com.bianbu.bad", "com.bianbu.c"], [])
    result = apply_reconciliation(plan, unregister, dry_run=False)
    assert result.removed == ["com.bianbu.a"]
    assert result.stopped is True
    assert result.failed == [
        {"agent_id": "com.bianbu.bad", "error": "securityd unavailable"},
    ]
    assert "com.bianbu.c" not in result.removed


def test_an_unconfirmed_unregister_is_a_failure_not_a_success():
    plan = plan_reconciliation(["com.bianbu.gone"], [])
    result = apply_reconciliation(plan, lambda _agent_id: False, dry_run=False)
    assert result.removed == []
    assert result.stopped is True
    assert result.failed[0]["agent_id"] == "com.bianbu.gone"


@pytest.mark.parametrize("rows, expected", [
    ([{"agent_id": ""}], ("",)),
    ([{"agent_id": "   "}], ("",)),
])
def test_blank_registry_rows_are_reported_instead_of_being_removed(rows, expected):
    plan = plan_reconciliation(rows, [])
    assert plan.unknown == expected
    assert plan.remove == ()
