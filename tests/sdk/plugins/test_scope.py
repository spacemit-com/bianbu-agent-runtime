from __future__ import annotations

from pathlib import Path

import pytest

from bianbu_agent.plugins import PluginContext
from bianbu_agent.plugins.contributions import SkillContribution, SkillSpec
from bianbu_agent.plugins.scope import PluginScope


@pytest.mark.asyncio
async def test_scope_owns_contributions_services_and_reverse_order_cleanup() -> None:
    events: list[str] = []
    shared: dict[str, object] = {"runtime": object()}
    scope = PluginScope("vendor.bundle", base_services=shared)
    skill = SkillContribution(
        plugin_id="vendor.bundle",
        spec=SkillSpec(name="review", instructions="Review."),
    )

    scope.register_skill(skill)
    scope.provide("vendor.bundle.index", {"ready": True})
    scope.effect(lambda: lambda: events.append("first"))

    async def dispose_second() -> None:
        events.append("second")

    scope.effect(lambda: dispose_second)
    scope.seal()

    assert scope.require("runtime") is shared["runtime"]
    assert scope.require("vendor.bundle.index") == {"ready": True}
    assert scope.snapshot().skills == (skill,)
    with pytest.raises(RuntimeError, match="sealed"):
        scope.register_skill(skill)

    errors = await scope.dispose()

    assert errors == ()
    assert events == ["second", "first"]
    assert "vendor.bundle.index" not in shared
    assert scope.snapshot().skills == ()


def test_context_exposes_scope_as_the_only_service_boundary(tmp_path: Path) -> None:
    service = object()
    scope = PluginScope("vendor.bundle", base_services={"runtime": service})
    context = PluginContext(
        plugin_id="vendor.bundle",
        data_dir=tmp_path,
        scope=scope,
    )

    assert context.require("runtime") is service
    with pytest.raises(KeyError, match="missing"):
        context.require("missing")


@pytest.mark.asyncio
async def test_scope_disposes_every_effect_when_one_cleanup_fails() -> None:
    events: list[str] = []
    scope = PluginScope("vendor.bundle")
    scope.effect(lambda: lambda: events.append("first"))

    def fail() -> None:
        events.append("failing")
        raise RuntimeError("cleanup failed")

    scope.effect(lambda: fail)
    scope.effect(lambda: lambda: events.append("last"))

    errors = await scope.dispose()

    assert events == ["last", "failing", "first"]
    assert len(errors) == 1
    assert str(errors[0]) == "cleanup failed"


@pytest.mark.asyncio
async def test_scope_event_handlers_are_generation_local_and_reversible() -> None:
    scope = PluginScope("vendor.bundle")
    seen: list[str] = []

    async def receive(value: str) -> None:
        seen.append(value)

    dispose = scope.on("document.indexed", receive)
    await scope.emit("document.indexed", "first")
    dispose()
    await scope.emit("document.indexed", "second")

    assert seen == ["first"]


def test_scope_exposes_services_only_to_declared_dependencies() -> None:
    services = {"index": object(), "secret": object()}
    owners = {"index": "vendor.index", "secret": "vendor.secret"}
    scope = PluginScope(
        "vendor.consumer",
        shared_services=services,
        shared_service_owners=owners,
        allowed_service_owners=frozenset({"vendor.index"}),
    )

    assert scope.require("index") is services["index"]
    assert "secret" not in scope.service_view
    with pytest.raises(PermissionError, match="undeclared dependency"):
        scope.require("secret")


@pytest.mark.asyncio
async def test_effect_disposer_is_idempotent() -> None:
    calls: list[str] = []
    scope = PluginScope("vendor.bundle")
    dispose = scope.effect(lambda: lambda: calls.append("disposed"))

    await dispose()
    await scope.dispose()

    assert calls == ["disposed"]
