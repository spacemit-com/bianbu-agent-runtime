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


@pytest.mark.asyncio
async def test_time_approval_is_private_and_owned_by_intent_hook():
    ticket = {"kind": "disable_ntp_and_set_time", "token": "secret-ticket", "id": "private-id"}

    async def handler(context):
        return ContextPatch(direct_reply="Confirm NTP", approval=ticket)

    events = []

    async def observe(event):
        events.append(event)

    generation = _generation((HookContribution("bianbu.bibit.intent", HookPoint.BEFORE_RUN, handler),))
    with observe_hooks(observe):
        patch = await generation.run_hooks(HookPoint.BEFORE_RUN, _context())
    assert patch.approval["token"] == "secret-ticket"
    assert "secret-ticket" not in repr(events) and "private-id" not in repr(events)
    other = _generation((HookContribution("vendor.other", HookPoint.BEFORE_RUN, handler),))
    assert (await other.run_hooks(HookPoint.BEFORE_RUN, _context())).approval is None


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


def _timed_hook(name, delay, *, priority, order):
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
        id=f"{name}.hook",
    )


@pytest.mark.asyncio
async def test_hooks_overlap_and_still_merge_in_priority_order() -> None:
    order: list[str] = []
    slow = _timed_hook("slow", 0.4, priority=100, order=order)
    fast = _timed_hook("fast", 0.1, priority=200, order=order)
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
async def test_a_slow_later_hook_does_not_delay_a_faster_one() -> None:
    """No Hook can make another wait: the batch is only as slow as its slowest."""

    order: list[str] = []
    quick = _timed_hook("quick", 0.05, priority=100, order=order)
    early = _timed_hook("early", 0.5, priority=100, order=order)
    late = _timed_hook("late", 0.05, priority=200, order=order)
    generation = _generation((quick, early, late))

    started = asyncio.get_running_loop().time()
    patch = await generation.run_hooks(
        HookPoint.AFTER_RUN,
        _context(),
        hook_ids=(
            "quick.plugin/quick.hook",
            "early.plugin/early.hook",
            "late.plugin/late.hook",
        ),
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.6
    # The later Hook starts at once instead of queueing behind `early`.
    assert order.index("late:start") < order.index("early:end")
    # Merge order stays (priority, hook id): same priority, so `early` precedes
    # `quick` even though `quick` finished first.
    assert patch.context_sections == ("early", "quick", "late")


@pytest.mark.asyncio
async def test_gate_denial_fails_the_batch_even_though_all_hooks_start() -> None:
    ran: list[str] = []

    async def deny(_context: RunContext) -> ContextPatch:
        raise PermissionError("denied by guard")

    async def later(_context: RunContext) -> ContextPatch:
        ran.append("later")
        return ContextPatch(context_sections=("later",))

    generation = _generation((
        HookContribution(
            "guard.plugin", HookPoint.BEFORE_TOOL, deny, priority=100,
            id="guard.hook",
        ),
        HookContribution(
            "after.plugin", HookPoint.BEFORE_TOOL, later, priority=200,
            id="after.hook",
        ),
    ))

    # Overlapping Hooks share the batch: the denial still fails the gate and
    # therefore the Run, while a sibling that already started is not rolled back.
    with pytest.raises(PermissionError, match="denied by guard"):
        await generation.run_hooks(
            HookPoint.BEFORE_TOOL,
            _context(),
            hook_ids=("guard.plugin/guard.hook", "after.plugin/after.hook"),
        )
    assert ran == ["later"]


@pytest.mark.asyncio
async def test_cancelling_run_hooks_cleans_up_pending_hooks() -> None:
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
            "slow.plugin", HookPoint.AFTER_RUN, slow, priority=100,
            id="slow.hook",
        ),
        HookContribution(
            "quick.plugin", HookPoint.AFTER_RUN, quick, priority=200,
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
@pytest.mark.parametrize("point", list(HookPoint))
async def test_denial_terminates_at_every_event_point(point: HookPoint) -> None:
    """A veto is not limited to "gate" events: the event name only says when."""

    async def deny(_context: RunContext) -> ContextPatch:
        raise PermissionError("denied by guard")

    generation = _generation((
        HookContribution("guard.plugin", point, deny, id="guard.hook"),
    ))

    with pytest.raises(PermissionError, match="denied by guard") as caught:
        await generation.run_hooks(point, _context())
    # A bare PermissionError from plugin code must still terminate the Run at
    # the tool boundary; the security middleware otherwise converts it into a
    # ToolMessage and lets the Agent try another path.
    assert caught.value.should_terminate_run is True


@pytest.mark.asyncio
async def test_non_gate_failure_stays_fail_open() -> None:
    async def broken(_context: RunContext) -> ContextPatch:
        raise ValueError("best effort")

    async def healthy(_context: RunContext) -> ContextPatch:
        return ContextPatch(context_sections=("healthy",))

    generation = _generation((
        HookContribution(
            "broken.plugin", HookPoint.AFTER_RUN, broken, priority=100,
            id="broken.hook",
        ),
        HookContribution(
            "healthy.plugin", HookPoint.AFTER_RUN, healthy, priority=200,
            id="healthy.hook",
        ),
    ))

    patch = await generation.run_hooks(
        HookPoint.AFTER_RUN,
        _context(),
        hook_ids=("broken.plugin/broken.hook", "healthy.plugin/healthy.hook"),
    )
    assert patch.context_sections == ("healthy",)
