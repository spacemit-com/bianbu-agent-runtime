from __future__ import annotations

import asyncio

import pytest

from bianbu_agent.plugins import (
    ContributionConflictError,
    ContextPatch,
    HookContribution,
    HookPoint,
    PluginContributions,
    PluginGeneration,
    PluginRuntime,
    RunContext,
)
from bianbu_agent.plugins.hook_observation import observe_hooks


def _generation(hooks: tuple[HookContribution, ...]) -> PluginGeneration:
    plugins = {}
    for plugin_id in {hook.plugin_id for hook in hooks}:
        plugins[plugin_id] = PluginRuntime(
            plugin_id=plugin_id,
            instance=object(),
            config={},
            contributions=PluginContributions(
                hooks=tuple(hook for hook in hooks if hook.plugin_id == plugin_id),
            ),
        )
    return PluginGeneration(id=1, plugins=plugins, statuses={})


def _context() -> RunContext:
    return RunContext(run_id="run", session_id="session", agent_id="agent", request="hi")


def test_hook_failure_text_never_empty() -> None:
    from types import SimpleNamespace

    from bianbu_agent.plugins.host import _failure_text

    contribution = SimpleNamespace(timeout_seconds=12.0)
    assert _failure_text(TimeoutError(), contribution) == "Hook timed out after 12s"
    assert _failure_text(ValueError("boom"), contribution) == "boom"
    assert _failure_text(RuntimeError(), contribution) == "RuntimeError"


@pytest.mark.asyncio
async def test_hook_observer_receives_paired_high_signal_execution_facts() -> None:
    observations: list[dict[str, object]] = []

    async def observe(event: dict[str, object]) -> None:
        observations.append(event)

    async def handler(_context: RunContext) -> ContextPatch:
        return ContextPatch(
            context_sections=("remembered preference",),
            metadata={"token": "must-not-be-persisted", "count": 2},
        )

    generation = _generation((
        HookContribution(
            "bianbu.memory",
            HookPoint.AFTER_CONTEXT,
            handler,
            id="memory.context",
            metadata={"qualified_id": "bianbu.memory/memory.context"},
        ),
    ))

    with observe_hooks(observe):
        result = await generation.run_hooks(
            HookPoint.AFTER_CONTEXT,
            _context(),
            hook_ids=("bianbu.memory/memory.context",),
        )

    assert result.context_sections == ("remembered preference",)
    assert [event["phase"] for event in observations] == ["invoked", "result"]
    assert observations[0] == {
        "phase": "invoked",
        "invocation_id": observations[0]["invocation_id"],
        "hook_id": "bianbu.memory/memory.context",
        "plugin_id": "bianbu.memory",
        "point": "after_context",
    }
    assert observations[1]["invocation_id"] == observations[0]["invocation_id"]
    assert observations[1]["status"] == "completed"
    assert observations[1]["context_sections"] == ["remembered preference"]
    assert observations[1]["metadata"] == {"token": "[REDACTED]", "count": 2}
    assert float(observations[1]["duration_ms"]) >= 0


@pytest.mark.asyncio
async def test_hook_observer_records_contained_hook_failure() -> None:
    observations: list[dict[str, object]] = []

    async def observe(event: dict[str, object]) -> None:
        observations.append(event)

    async def broken(_context: RunContext) -> ContextPatch:
        raise RuntimeError("boom")

    generation = _generation((
        HookContribution("vendor.audit", HookPoint.AFTER_RUN, broken),
    ))

    with observe_hooks(observe):
        result = await generation.run_hooks(HookPoint.AFTER_RUN, _context())

    assert result == ContextPatch()
    assert [event["phase"] for event in observations] == ["invoked", "result"]
    assert observations[1]["status"] == "failed"
    assert observations[1]["error"] == "boom"


@pytest.mark.asyncio
async def test_hooks_run_by_priority_then_plugin_id_and_merge_patches() -> None:
    calls: list[str] = []

    def handler(name: str, section: str):
        async def run(_context: RunContext) -> ContextPatch:
            calls.append(name)
            return ContextPatch(
                context_sections=(section,),
                metadata={"value": name},
            )

        return run

    generation = _generation(
        (
            HookContribution("z.plugin", HookPoint.AFTER_CONTEXT, handler("z", "second"), 20),
            HookContribution("a.plugin", HookPoint.AFTER_CONTEXT, handler("a", "first"), 20),
            HookContribution("m.plugin", HookPoint.BEFORE_RUN, handler("m", "ignored"), 1),
        )
    )

    result = await generation.run_hooks(HookPoint.AFTER_CONTEXT, _context())

    assert calls == ["a", "z"]
    assert result.context_sections == ("first", "second")
    assert result.metadata == {
        "a.plugin": {"value": "a"},
        "z.plugin": {"value": "z"},
    }


@pytest.mark.asyncio
async def test_empty_hook_ids_skip_every_hook_without_legacy_metadata() -> None:
    calls: list[str] = []

    def handler(name: str):
        async def run(_context):
            calls.append(name)
            return ContextPatch()

        return run

    generation = _generation(
        (
            HookContribution(
                "bianbu.memory",
                HookPoint.BEFORE_RUN,
                handler("memory"),
                id="memory.before-run",
                metadata={"qualified_id": "bianbu.memory/memory.before-run"},
            ),
            HookContribution(
                "bianbu.security",
                HookPoint.BEFORE_RUN,
                handler("security"),
                id="security.before-run",
                metadata={"qualified_id": "bianbu.security/security.before-run"},
            ),
        )
    )

    result = await generation.run_hooks(HookPoint.BEFORE_RUN, _context(), hook_ids=())

    assert calls == []
    assert result == ContextPatch()


@pytest.mark.asyncio
async def test_exact_hook_ids_run_only_matching_qualified_hook_without_legacy_metadata() -> None:
    calls: list[str] = []

    def handler(name: str):
        async def run(_context):
            calls.append(name)
            return ContextPatch(context_sections=(name,))

        return run

    generation = _generation(
        (
            HookContribution(
                "bianbu.memory",
                HookPoint.BEFORE_RUN,
                handler("memory"),
                id="memory.before-run",
                metadata={"qualified_id": "bianbu.memory/memory.before-run"},
            ),
            HookContribution(
                "bianbu.security",
                HookPoint.BEFORE_RUN,
                handler("security"),
                id="security.before-run",
                metadata={"qualified_id": "bianbu.security/security.before-run"},
            ),
        )
    )

    result = await generation.run_hooks(
        HookPoint.BEFORE_RUN,
        _context(),
        hook_ids=("bianbu.security/security.before-run",),
    )

    assert calls == ["security"]
    assert result.context_sections == ("security",)


@pytest.mark.asyncio
async def test_none_hook_ids_retains_low_level_run_all_behavior() -> None:
    calls: list[str] = []

    def handler(name: str):
        async def run(_context):
            calls.append(name)
            return ContextPatch()

        return run

    generation = _generation(
        (
            HookContribution(
                "bianbu.memory",
                HookPoint.BEFORE_RUN,
                handler("memory"),
                id="memory.before-run",
                metadata={"qualified_id": "bianbu.memory/memory.before-run"},
            ),
            HookContribution(
                "bianbu.security",
                HookPoint.BEFORE_RUN,
                handler("security"),
                id="security.before-run",
                metadata={"qualified_id": "bianbu.security/security.before-run"},
            ),
        )
    )

    await generation.run_hooks(HookPoint.BEFORE_RUN, _context(), hook_ids=None)

    assert calls == ["memory", "security"]


@pytest.mark.asyncio
async def test_plugin_hooks_with_same_local_id_keep_canonical_selection_ids() -> None:
    calls: list[str] = []

    def contribution(owner: str) -> HookContribution:
        async def run(_context):
            calls.append(owner)
            return ContextPatch()

        return HookContribution(
            owner,
            HookPoint.BEFORE_RUN,
            run,
            id="shared.before-run",
            metadata={"agent_selectable": True},
        )

    def runtime(owner: str) -> PluginRuntime:
        return PluginRuntime(
            plugin_id=owner,
            instance=object(),
            config={},
            contributions=PluginContributions(hooks=(contribution(owner),)),
        )

    generation = PluginGeneration(
        id=1,
        plugins={
            "first.plugin": runtime("first.plugin"),
            "second.plugin": runtime("second.plugin"),
        },
        statuses={},
    )

    await generation.run_hooks(
        HookPoint.BEFORE_RUN,
        _context(),
        hook_ids=("first.plugin/shared.before-run",),
    )

    assert calls == ["first.plugin"]
    assert set(generation.hooks) == {
        "first.plugin/shared.before-run",
        "second.plugin/shared.before-run",
    }


def test_dynamic_plugin_hook_is_qualified_without_a_file_backed_catalog() -> None:
    async def handler(_context):
        return ContextPatch()

    generation = PluginGeneration(
        id=1,
        plugins={
            "vendor.audit": PluginRuntime(
                plugin_id="vendor.audit",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(
                        HookContribution(
                            "vendor.audit",
                            HookPoint.BEFORE_RUN,
                            handler,
                            id="shared.before_run",
                        ),
                    )
                ),
            )
        },
        statuses={},
    )

    assert tuple(generation.hooks) == ("vendor.audit/shared.before_run",)
    assert generation.hooks["vendor.audit/shared.before_run"].metadata[
        "qualified_id"
    ] == "vendor.audit/shared.before_run"


@pytest.mark.asyncio
async def test_hook_failure_and_timeout_skip_only_failed_patch(caplog) -> None:
    async def fail(_context):
        raise RuntimeError("boom")

    async def slow(_context):
        await asyncio.sleep(1)

    async def succeed(_context):
        return ContextPatch(context_sections=("ok",))

    generation = _generation(
        (
            HookContribution("a.fail", HookPoint.BEFORE_MODEL, fail),
            HookContribution("b.slow", HookPoint.BEFORE_MODEL, slow, timeout_seconds=0.001),
            HookContribution("c.good", HookPoint.BEFORE_MODEL, succeed),
        )
    )

    result = await generation.run_hooks(HookPoint.BEFORE_MODEL, _context())

    assert result.context_sections == ("ok",)
    assert "hook failed" in caplog.text


@pytest.mark.asyncio
async def test_before_run_hook_aggregates_direct_reply() -> None:
    async def direct(_context):
        return ContextPatch(direct_reply="hello")

    result = await _generation(
        (HookContribution("a.direct", HookPoint.BEFORE_RUN, direct),)
    ).run_hooks(HookPoint.BEFORE_RUN, _context())

    assert result.direct_reply == "hello"


@pytest.mark.asyncio
async def test_two_direct_replies_raise_deterministic_conflict() -> None:
    async def direct(_context):
        return ContextPatch(direct_reply="hello")

    with pytest.raises(
        ContributionConflictError,
        match="multiple direct replies from hooks: a.direct/a.direct, z.direct/z.direct",
    ):
        await _generation(
            (
                HookContribution("z.direct", HookPoint.BEFORE_RUN, direct),
                HookContribution("a.direct", HookPoint.BEFORE_RUN, direct),
            )
        ).run_hooks(HookPoint.BEFORE_RUN, _context())


@pytest.mark.asyncio
async def test_same_owner_hooks_use_contribution_id_for_tie_ordering() -> None:
    calls: list[str] = []

    def handler(name: str):
        async def run(_context):
            calls.append(name)
            return ContextPatch()

        return run

    await _generation(
        (
            HookContribution(
                "bianbu.catalog.filesystem",
                HookPoint.BEFORE_RUN,
                handler("z"),
                priority=10,
                id="z.hook",
            ),
            HookContribution(
                "bianbu.catalog.filesystem",
                HookPoint.BEFORE_RUN,
                handler("a"),
                priority=10,
                id="a.hook",
            ),
        )
    ).run_hooks(HookPoint.BEFORE_RUN, _context())

    assert calls == ["a", "z"]


@pytest.mark.asyncio
async def test_same_owner_direct_reply_conflict_names_contribution_ids() -> None:
    async def direct(_context):
        return ContextPatch(direct_reply="hello")

    with pytest.raises(
        ContributionConflictError,
        match=(
            "multiple direct replies from hooks: "
            "bianbu.catalog.filesystem/a.direct, "
            "bianbu.catalog.filesystem/z.direct"
        ),
    ):
        await _generation(
            (
                HookContribution(
                    "bianbu.catalog.filesystem",
                    HookPoint.BEFORE_RUN,
                    direct,
                    id="z.direct",
                ),
                HookContribution(
                    "bianbu.catalog.filesystem",
                    HookPoint.BEFORE_RUN,
                    direct,
                    id="a.direct",
                ),
            )
        ).run_hooks(HookPoint.BEFORE_RUN, _context())


@pytest.mark.asyncio
async def test_direct_reply_at_other_hook_point_is_isolated_as_invalid_patch(caplog) -> None:
    async def invalid(_context):
        return ContextPatch(direct_reply="hello", context_sections=("ignored",))

    result = await _generation(
        (HookContribution("a.direct", HookPoint.AFTER_CONTEXT, invalid),)
    ).run_hooks(HookPoint.AFTER_CONTEXT, _context())

    assert result == ContextPatch()
    assert "error=invalid patch" in caplog.text


def _timed_hook(name, delay, *, priority, parallel, order, waits_for=()):
    async def handler(_context: RunContext) -> ContextPatch:
        order.append(f"{name}:start")
        await asyncio.sleep(delay)
        order.append(f"{name}:end")
        return ContextPatch(context_sections=(name,))

    return HookContribution(
        f"{name}.plugin",
        HookPoint.AFTER_RUN,
        handler,
        priority=priority,
        parallel=parallel,
        id=f"{name}.hook",
        waits_for=waits_for,
    )


@pytest.mark.asyncio
async def test_waits_for_only_delays_the_named_hooks() -> None:
    """A device-side Hook waits for the gate but not for best-effort Hooks."""

    order: list[str] = []
    guard = _timed_hook("guard", 0.2, priority=100, parallel=True, order=order)
    recall = _timed_hook("recall", 0.5, priority=100, parallel=True, order=order)
    device = _timed_hook(
        "device",
        0.2,
        priority=200,
        parallel=True,
        order=order,
        waits_for=("guard.plugin/*",),
    )
    generation = _generation((guard, recall, device))

    started = asyncio.get_running_loop().time()
    patch = await generation.run_hooks(
        HookPoint.AFTER_RUN,
        _context(),
        hook_ids=(
            "guard.plugin/guard.hook",
            "recall.plugin/recall.hook",
            "device.plugin/device.hook",
        ),
    )
    elapsed = asyncio.get_running_loop().time() - started

    # Serial would be 0.2 + 0.5 + 0.2 = 0.9s; only the gate is waited for, so
    # the batch costs max(0.5, 0.2 + 0.2) = 0.5s.
    assert elapsed < 0.7
    assert order.index("device:start") > order.index("guard:end")
    assert order.index("device:start") < order.index("recall:end")
    # Merging still follows (priority, hook id), not completion order.
    assert patch.context_sections == ("guard", "recall", "device")


@pytest.mark.asyncio
async def test_waits_for_ignores_dependencies_that_sort_later() -> None:
    """A pattern that only matches a later Hook must not deadlock the batch."""

    order: list[str] = []
    early = _timed_hook(
        "early", 0.05, priority=100, parallel=True, order=order,
        waits_for=("late.plugin/*",),
    )
    late = _timed_hook("late", 0.05, priority=200, parallel=True, order=order)
    generation = _generation((early, late))

    patch = await asyncio.wait_for(
        generation.run_hooks(
            HookPoint.AFTER_RUN,
            _context(),
            hook_ids=("early.plugin/early.hook", "late.plugin/late.hook"),
        ),
        timeout=2,
    )

    assert patch.context_sections == ("early", "late")


def test_waits_for_requires_parallel() -> None:
    async def handler(_context: RunContext) -> ContextPatch:
        return ContextPatch()

    with pytest.raises(ValueError, match="waits_for requires parallel"):
        HookContribution(
            "device.plugin",
            HookPoint.BEFORE_RUN,
            handler,
            id="device.hook",
            waits_for=("bianbu.security/*",),
        )


@pytest.mark.asyncio
async def test_parallel_hooks_overlap_and_still_merge_in_priority_order() -> None:
    order: list[str] = []
    slow = _timed_hook("slow", 0.4, priority=100, parallel=True, order=order)
    fast = _timed_hook("fast", 0.1, priority=200, parallel=True, order=order)
    generation = _generation((slow, fast))

    started = asyncio.get_running_loop().time()
    patch = await generation.run_hooks(
        HookPoint.AFTER_RUN,
        _context(),
        hook_ids=("slow.plugin/slow.hook", "fast.plugin/fast.hook"),
    )
    elapsed = asyncio.get_running_loop().time() - started

    # Sum of the two budgets is 0.5s; overlapping them costs the longest one.
    assert elapsed < 0.45
    # `fast` finishes first, yet the merged patch keeps (priority, hook id).
    assert order == ["slow:start", "fast:start", "fast:end", "slow:end"]
    assert patch.context_sections == ("slow", "fast")


@pytest.mark.asyncio
async def test_parallel_gate_denial_stops_later_serial_hooks() -> None:
    ran: list[str] = []

    async def deny(_context: RunContext) -> ContextPatch:
        raise PermissionError("denied by guard")

    async def later(_context: RunContext) -> ContextPatch:
        ran.append("later")
        return ContextPatch(context_sections=("later",))

    generation = _generation((
        HookContribution(
            "guard.plugin", HookPoint.BEFORE_TOOL, deny, priority=100,
            parallel=True, id="guard.hook",
        ),
        HookContribution(
            "after.plugin", HookPoint.BEFORE_TOOL, later, priority=200,
            id="after.hook",
        ),
    ))

    with pytest.raises(PermissionError, match="denied by guard"):
        await generation.run_hooks(
            HookPoint.BEFORE_TOOL,
            _context(),
            hook_ids=("guard.plugin/guard.hook", "after.plugin/after.hook"),
        )
    assert ran == []


@pytest.mark.asyncio
async def test_cancelling_run_hooks_cleans_up_pending_parallel_hooks() -> None:
    cleaned: list[str] = []

    async def slow(_context: RunContext) -> ContextPatch:
        try:
            await asyncio.sleep(30)
        finally:
            cleaned.append("slow")
        return ContextPatch()

    async def quick(_context: RunContext) -> ContextPatch:
        await asyncio.sleep(30)
        return ContextPatch()

    generation = _generation((
        HookContribution(
            "slow.plugin", HookPoint.AFTER_RUN, slow, priority=100, parallel=True,
            id="slow.hook",
        ),
        HookContribution(
            "quick.plugin", HookPoint.AFTER_RUN, quick, priority=200, parallel=True,
            id="quick.hook",
        ),
    ))

    task = asyncio.create_task(generation.run_hooks(
        HookPoint.AFTER_RUN,
        _context(),
        hook_ids=("slow.plugin/slow.hook", "quick.plugin/quick.hook"),
    ))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == ["slow"]


@pytest.mark.asyncio
async def test_parallel_non_gate_failure_stays_fail_open() -> None:
    async def broken(_context: RunContext) -> ContextPatch:
        raise ValueError("best effort")

    async def healthy(_context: RunContext) -> ContextPatch:
        return ContextPatch(context_sections=("healthy",))

    generation = _generation((
        HookContribution(
            "broken.plugin", HookPoint.AFTER_RUN, broken, priority=100,
            parallel=True, id="broken.hook",
        ),
        HookContribution(
            "healthy.plugin", HookPoint.AFTER_RUN, healthy, priority=200,
            parallel=True, id="healthy.hook",
        ),
    ))

    patch = await generation.run_hooks(
        HookPoint.AFTER_RUN,
        _context(),
        hook_ids=("broken.plugin/broken.hook", "healthy.plugin/healthy.hook"),
    )
    assert patch.context_sections == ("healthy",)
