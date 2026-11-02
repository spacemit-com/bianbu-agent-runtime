"""Reconcile the securityd Agent registry against the Runtime inventory.

The Runtime and securityd own two registries.  Deleting one Expert removes both
entries, but an interrupted upgrade, a re-imaged database or a crashed process
can leave orphaned identities behind.  Resetting the whole registry is not a
fix: securityd also stores service principals and Agents registered by other
applications, and unregistering an active Agent drops its capability rows,
bindings and session trust while its Worker is still running.

This module therefore only removes *orphans*: identities that securityd lists,
that no Runtime Expert declares, that are not protected service principals and
that have no live Worker.  It is a pure planner plus a thin executor so the
dry-run and the real run share exactly the same decision code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    """Decision for every identity securityd currently exposes."""

    remove: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()
    service: tuple[str, ...] = ()
    active: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "would_remove": list(self.remove),
            "kept": list(self.keep),
            "skipped_service": list(self.service),
            "skipped_active": list(self.active),
            "skipped_invalid": list(self.unknown),
        }


def plan_reconciliation(
    registered: Iterable[object],
    experts: Iterable[str],
    *,
    keep: Iterable[str] = (),
    active: Iterable[str] = (),
    is_service: Callable[[str], bool] = lambda _agent_id: False,
    managed_prefix: str | None = "com.bianbu.",
) -> ReconcilePlan:
    """Classify registered identities without changing anything.

    ``registered`` is securityd's list.  Entries may be mapping rows or plain
    IDs.  ``experts`` is the authoritative Runtime inventory.  Any registered
    ID that is neither declared, explicitly kept, a service principal nor
    active is an orphan and becomes removable.  ``managed_prefix`` scopes the
    cleanup to one namespace: securityd is also the registry for Hermes, DSH
    and other products, and their Agents must not disappear because this
    Runtime does not declare them.  ``None`` disables the namespace filter.
    """

    declared = {_identity(item) for item in experts}
    declared.discard("")
    protected = {_identity(item) for item in keep}
    protected.discard("")
    busy = {_identity(item) for item in active}
    busy.discard("")
    removals: list[str] = []
    kept: list[str] = []
    service: list[str] = []
    running: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for row in registered:
        agent_id = _identity(row)
        if not agent_id:
            unknown.append("")
            continue
        if agent_id in seen:
            continue
        seen.add(agent_id)
        if agent_id in declared or agent_id in protected:
            kept.append(agent_id)
        elif managed_prefix is not None and not agent_id.startswith(managed_prefix):
            # Another product's Agent.  It is not this Runtime's inventory to
            # reconcile, so report it as kept instead of deleting it.
            kept.append(agent_id)
        elif is_service(agent_id):
            service.append(agent_id)
        elif agent_id in busy:
            # A live Worker still reads this Agent's policies and trust.  Leave
            # the row alone; the next reconcile after it stops will remove it.
            running.append(agent_id)
        else:
            removals.append(agent_id)
    return ReconcilePlan(
        remove=tuple(sorted(removals)),
        keep=tuple(sorted(kept)),
        service=tuple(sorted(service)),
        active=tuple(sorted(running)),
        unknown=tuple(unknown),
    )


@dataclass(slots=True)
class ReconcileResult:
    plan: ReconcilePlan
    dry_run: bool
    removed: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    stopped: bool = False

    def as_dict(self) -> dict[str, object]:
        result = self.plan.as_dict()
        result.update({
            "dry_run": self.dry_run,
            "removed": list(self.removed),
            "failed": list(self.failed),
            "stopped": self.stopped,
        })
        return result


def apply_reconciliation(
    plan: ReconcilePlan,
    unregister: Callable[[str], bool],
    *,
    dry_run: bool = True,
) -> ReconcileResult:
    """Execute ``plan`` one identity at a time.

    The first failure stops the run.  Continuing would report a clean registry
    while earlier rows are gone and later ones remain, which is harder to audit
    than a clearly partial result.
    """

    result = ReconcileResult(plan=plan, dry_run=bool(dry_run))
    if result.dry_run:
        return result
    for agent_id in plan.remove:
        try:
            removed = unregister(agent_id)
        except Exception as exc:  # noqa: BLE001 - report the exact failing ID
            result.failed.append({"agent_id": agent_id, "error": str(exc)})
            result.stopped = True
            break
        if not removed:
            result.failed.append({
                "agent_id": agent_id,
                "error": "securityd did not confirm unregistering this Agent",
            })
            result.stopped = True
            break
        result.removed.append(agent_id)
    return result


def _identity(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return str(value.get("agent_id") or "").strip()
    nested = getattr(value, "id", None)
    return str(nested or "").strip()


__all__ = [
    "ReconcilePlan",
    "ReconcileResult",
    "apply_reconciliation",
    "plan_reconciliation",
]
