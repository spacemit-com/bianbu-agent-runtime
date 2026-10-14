from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from bianbu_agent.plugins.contributions import PluginContributions
from bianbu_agent.plugins.contributions import (
    ContextPatch,
    HookContribution,
    HookPoint,
    MCPRuntimeContribution,
    RunContext,
    SkillContribution,
    ToolContribution,
)
from bianbu_agent.plugins.models import (
    failure_message,
    Plugin,
    PluginConfigError,
    PluginContext,
    PluginDiagnostic,
    PluginHealth,
    PluginState,
)
from bianbu_agent.plugins.hook_observation import emit_hook_observation
from bianbu_agent.plugins.registry import PluginCandidate, PluginRegistry
from bianbu_agent.plugins.scope import EventHandler, PluginScope
from bianbu_agent._internal.filesystem import digest_directory, regular_files

if TYPE_CHECKING:
    from bianbu_agent.inventory import CapabilityCatalog


logger = logging.getLogger(__name__)


def _hook_id(contribution: HookContribution) -> str:
    return str(contribution.metadata.get("qualified_id") or contribution.id)


def _tool_unavailable_reason(contribution: ToolContribution) -> str:
    return str(contribution.spec.metadata.get("unavailable_reason") or "").strip()


_GATE_POINTS = frozenset({
    HookPoint.BEFORE_RUN,
    HookPoint.BEFORE_TOOL,
    HookPoint.BEFORE_MODEL,
})


def _failure_text(exc: BaseException, contribution: HookContribution) -> str:
    """Never hand the Console an empty Hook failure reason.

    ``str(TimeoutError())`` is empty and the contribution-level budget is
    enforced here, so name that budget instead of dropping the cause.
    """
    text = str(exc).strip()
    if text:
        return text
    if isinstance(exc, TimeoutError):
        return f"Hook timed out after {contribution.timeout_seconds:g}s"
    return type(exc).__name__


class ContributionConflictError(ValueError):
    """Raised when one immutable generation has ambiguous contributions."""


@dataclass(frozen=True, slots=True)
class PluginStatus:
    plugin_id: str
    state: PluginState
    health: PluginHealth
    diagnostics: tuple[PluginDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginRuntime:
    plugin_id: str
    instance: Plugin
    config: Mapping[str, object]
    contributions: PluginContributions
    config_provenance: Mapping[str, str] = field(default_factory=dict)
    scope: PluginScope | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class MCPRuntimeView:
    id: str
    runtime_id: str
    display_name: str
    owner_plugin_id: str | None
    source_kind: str
    transport: str
    command: str
    status: str
    tools: tuple[str, ...]
    tool_views: tuple[Mapping[str, object], ...] = ()


async def _invoke_hook(
    point: HookPoint,
    contribution: HookContribution,
    context: RunContext,
) -> tuple[ContextPatch | None, BaseException | None]:
    """Execute one Hook and report its patch or its failure.

    Failure policy stays with the caller: overlapping Hooks must keep the same
    fail-closed gates and fail-open observers as a serial pass, and the first
    failure in (priority, hook id) order has to be the one that surfaces.
    """

    invocation_id = uuid.uuid4().hex
    common: dict[str, object] = {
        "invocation_id": invocation_id,
        "hook_id": _hook_id(contribution),
        "plugin_id": contribution.plugin_id,
        "point": point.value,
    }
    await emit_hook_observation({"phase": "invoked", **common})
    started = time.perf_counter()
    try:
        async with asyncio.timeout(contribution.timeout_seconds):
            patch = await contribution.handler(context)
    except BaseException as exc:  # noqa: BLE001 - the caller decides the policy
        await emit_hook_observation({
            "phase": "result",
            **common,
            "status": "failed",
            "duration_ms": (time.perf_counter() - started) * 1000,
            "error": _failure_text(exc, contribution),
        })
        return None, exc
    if patch is None:
        await emit_hook_observation({
            "phase": "result",
            **common,
            "status": "completed",
            "duration_ms": (time.perf_counter() - started) * 1000,
        })
        return None, None
    invalid = (
        "Hook returned an invalid ContextPatch"
        if not isinstance(patch, ContextPatch)
        else "direct_reply is only valid for before_run"
        if patch.direct_reply is not None and point is not HookPoint.BEFORE_RUN
        else None
    )
    if invalid is not None:
        await emit_hook_observation({
            "phase": "result",
            **common,
            "status": "invalid",
            "duration_ms": (time.perf_counter() - started) * 1000,
            "error": invalid,
        })
        logger.warning(
            "plugin hook failed plugin=%s point=%s error=invalid patch",
            contribution.plugin_id,
            point.value,
        )
        return None, None
    await emit_hook_observation({
        "phase": "result",
        **common,
        "status": "completed",
        "duration_ms": (time.perf_counter() - started) * 1000,
        **(
            {"context_sections": list(patch.context_sections)}
            if patch.context_sections else {}
        ),
        **({"metadata": dict(patch.metadata)} if patch.metadata else {}),
        **({"direct_reply": True} if patch.direct_reply is not None else {}),
    })
    return patch, None


@dataclass(frozen=True, slots=True)
class PluginGeneration:
    id: int
    plugins: Mapping[str, PluginRuntime]
    statuses: Mapping[str, PluginStatus]
    diagnostics: tuple[PluginDiagnostic, ...] = ()
    capability_catalog: CapabilityCatalog | None = None
    _start_order: tuple[str, ...] = field(default=(), repr=False)
    tools: Mapping[str, ToolContribution] = field(init=False)
    skills: Mapping[str, SkillContribution] = field(init=False)
    hooks: Mapping[str, HookContribution] = field(init=False)
    mcps: Mapping[str, MCPRuntimeView] = field(init=False)
    mcp_tools: Mapping[str, tuple[str, ...]] = field(init=False)
    _hooks: Mapping[HookPoint, tuple[object, ...]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        plugins = dict(self.plugins)
        tools: dict[str, ToolContribution] = (
            dict(self.capability_catalog.tools)
            if self.capability_catalog is not None
            else {}
        )
        skills: dict[str, SkillContribution] = (
            dict(self.capability_catalog.skills)
            if self.capability_catalog is not None
            else {}
        )
        hooks: dict[HookPoint, list[object]] = {point: [] for point in HookPoint}
        for plugin_id in sorted(plugins):
            contributions = plugins[plugin_id].contributions
            if (
                contributions.tools
                and self.capability_catalog is not None
                and plugin_id not in self.capability_catalog.plugin_sources
                and not self._mcp_file_backed(plugin_id, contributions)
            ):
                raise ValueError(
                    f"plugin {plugin_id} is not backed by the file-backed catalog"
                )
            for contribution in contributions.tools:
                self._validate_owner(plugin_id, contribution.plugin_id)
                name = str(
                    contribution.spec.metadata.get("qualified_id")
                    or contribution.spec.name
                )
                self._add_named(tools, name, contribution, "tool")
            for contribution in contributions.skills:
                self._validate_owner(plugin_id, contribution.plugin_id)
                name = str(
                    contribution.spec.metadata.get("qualified_id")
                    or contribution.spec.name
                )
                self._add_named(skills, name, contribution, "skill")
            for contribution in contributions.hooks:
                self._validate_owner(plugin_id, contribution.plugin_id)
                qualified_id = f"{plugin_id}/{contribution.id}"
                if contribution.metadata.get("qualified_id") != qualified_id:
                    contribution = replace(
                        contribution,
                        metadata={
                            **dict(contribution.metadata),
                            "local_id": contribution.id,
                            "qualified_id": qualified_id,
                        },
                    )
                hooks[contribution.point].append(contribution)
        tools = {
            name: contribution
            for name, contribution in tools.items()
            if not _tool_unavailable_reason(contribution)
        }
        ordered_hooks = {
            point: tuple(
                sorted(items, key=lambda item: (item.priority, _hook_id(item)))
            )
            for point, items in hooks.items()
        }
        hook_by_id: dict[str, HookContribution] = {}
        for contributions in ordered_hooks.values():
            for contribution in contributions:
                self._add_named(
                    hook_by_id,
                    _hook_id(contribution),
                    contribution,
                    "hook",
                )
        grouped_mcp_tools: dict[str, list[str]] = {}
        for name, contribution in tools.items():
            metadata = contribution.spec.metadata
            if metadata.get("origin") != "mcp":
                continue
            mcp_id = str(metadata.get("mcp_id") or "")
            if not mcp_id:
                raise ValueError(f"MCP Tool {name} is missing mcp_id metadata")
            grouped_mcp_tools.setdefault(mcp_id, []).append(name)
        mcp_tools = {
            mcp_id: tuple(sorted(names))
            for mcp_id, names in grouped_mcp_tools.items()
        }
        mcps: dict[str, MCPRuntimeView] = {}
        for runtime_id, runtime in plugins.items():
            contribution = runtime.contributions.mcp_runtime
            if contribution is None:
                continue
            mcp_id = contribution.mcp_id
            if mcp_id in mcps:
                raise ContributionConflictError(f"duplicate MCP server ID: {mcp_id}")
            status = self.statuses.get(runtime_id)
            mcps[mcp_id] = MCPRuntimeView(
                id=mcp_id,
                runtime_id=runtime_id,
                display_name=contribution.display_name,
                owner_plugin_id=contribution.owner_plugin_id,
                source_kind=contribution.source_kind,
                transport=contribution.transport,
                command=contribution.command,
                status=(status.health.status if status is not None else "unknown"),
                tools=mcp_tools.get(mcp_id, ()),
                tool_views=contribution.tools,
            )
        object.__setattr__(self, "plugins", MappingProxyType(plugins))
        object.__setattr__(self, "statuses", MappingProxyType(dict(self.statuses)))
        object.__setattr__(self, "tools", MappingProxyType(tools))
        object.__setattr__(self, "skills", MappingProxyType(skills))
        object.__setattr__(self, "hooks", MappingProxyType(hook_by_id))
        object.__setattr__(self, "mcps", MappingProxyType(mcps))
        object.__setattr__(self, "mcp_tools", MappingProxyType(mcp_tools))
        object.__setattr__(self, "_hooks", MappingProxyType(ordered_hooks))

    def _mcp_file_backed(
        self,
        plugin_id: str,
        contributions: PluginContributions,
    ) -> bool:
        if contributions.skills or self.capability_catalog is None:
            return False
        source = self.capability_catalog.mcp_sources.get(plugin_id)
        if source is None:
            return False
        expected = (source.root / "mcp.json").resolve()
        if expected.parent != source.root or not expected.is_file() or expected.is_symlink():
            return False
        files = tuple(
            path
            for path in regular_files(source.root)
            if path.relative_to(source.root).parts[:1] != ("data",)
        )
        if digest_directory(source.root, files) != source.content_digest:
            return False
        metadata = [tool.spec.metadata for tool in contributions.tools]
        runtime = contributions.mcp_runtime
        if runtime is not None:
            metadata.append({
                "origin": "mcp",
                "plugin_id": runtime.plugin_id,
                "source_path": runtime.source_path,
            })
        return bool(metadata) and all(
            item.get("origin") == "mcp"
            and item.get("plugin_id") == plugin_id
            and Path(str(item.get("source_path") or "")).resolve() == expected
            for item in metadata
        )

    @property
    def inventory_revision(self) -> int:
        """Inventory revision from which this immutable generation was built."""

        if self.capability_catalog is None:
            return 0
        return self.capability_catalog.inventory_revision

    @property
    def dependency_order(self) -> tuple[str, ...]:
        return self._start_order

    @staticmethod
    def _validate_owner(plugin_id: str, contribution_plugin_id: str) -> None:
        if plugin_id != contribution_plugin_id:
            raise ValueError(
                f"contribution owner {contribution_plugin_id} does not match runtime {plugin_id}"
            )

    @staticmethod
    def _add_named(target: dict, name: str, contribution: object, kind: str) -> None:
        existing = target.get(name)
        if existing is not None:
            raise ContributionConflictError(
                f"duplicate {kind} contribution name: {name}"
            )
        target[name] = contribution

    async def run_hooks(
        self,
        point: HookPoint,
        context: RunContext,
        *,
        hook_ids: Collection[str] | None = None,
    ) -> ContextPatch:
        sections: list[str] = []
        metadata: dict[str, dict[str, object]] = {}
        direct_reply: str | None = None
        direct_reply_hook: str | None = None
        selected = None if hook_ids is None else frozenset(hook_ids)
        contributions = [
            contribution
            for contribution in self._hooks[HookPoint(point)]
            if selected is None or _hook_id(contribution) in selected
        ]
        # Hooks that declare ``parallel`` overlap with each other; the rest run
        # inline in the same (priority, hook id) order.  Patches are merged in
        # that order either way, so opting in changes latency, not the merged
        # result.  The batch only starts once the loop reaches it, which keeps a
        # denying Hook that sorts earlier from racing a Hook it should stop.
        parallel = [
            index
            for index, contribution in enumerate(contributions)
            if contribution.parallel
        ]
        first_parallel = parallel[0] if parallel else None
        pending: dict[
            int, asyncio.Task[tuple[ContextPatch | None, BaseException | None]]
        ] = {}
        finished = [asyncio.Event() for _ in contributions]
        # A Hook may declare that it only needs to wait for part of the batch
        # (``waits_for``): the Security decision must precede a device-side
        # Hook, but a best-effort recall Hook does not have to.  Only Hooks
        # that sort earlier can be waited for, because the merged patch order
        # is (priority, hook id) either way.
        waits = [
            tuple(
                other
                for other in range(index)
                if any(
                    fnmatch.fnmatchcase(_hook_id(contributions[other]), pattern)
                    for pattern in contribution.waits_for
                )
            )
            for index, contribution in enumerate(contributions)
        ]

        async def invoke_index(
            index: int,
        ) -> tuple[ContextPatch | None, BaseException | None]:
            """Run one Hook once everything it declared a dependency on finished."""

            try:
                for dependency in waits[index]:
                    await finished[dependency].wait()
                return await _invoke_hook(point, contributions[index], context)
            finally:
                finished[index].set()

        try:
            for index, contribution in enumerate(contributions):
                if index == first_parallel:
                    for position in parallel:
                        pending[position] = asyncio.create_task(
                            invoke_index(position)
                        )
                if index in pending:
                    patch, failure = await pending.pop(index)
                else:
                    patch, failure = await invoke_index(index)
                if failure is not None:
                    if isinstance(
                        failure,
                        (KeyboardInterrupt, SystemExit, asyncio.CancelledError),
                    ):
                        raise failure
                    if isinstance(failure, PermissionError) and point in _GATE_POINTS:
                        raise failure
                    logger.warning(
                        "plugin hook failed plugin=%s point=%s error=%s",
                        contribution.plugin_id,
                        point.value,
                        _failure_text(failure, contribution),
                    )
                    continue
                if patch is None:
                    continue
                if patch.direct_reply is not None:
                    if direct_reply is not None:
                        raise ContributionConflictError(
                            "multiple direct replies from hooks: "
                            f"{direct_reply_hook}, {_hook_id(contribution)}"
                        )
                    direct_reply = patch.direct_reply
                    direct_reply_hook = _hook_id(contribution)
                sections.extend(patch.context_sections)
                plugin_metadata = metadata.setdefault(contribution.plugin_id, {})
                plugin_metadata.update(patch.metadata)
        finally:
            for task in pending.values():
                task.cancel()
            if pending:
                await asyncio.gather(*pending.values(), return_exceptions=True)
        return ContextPatch(
            context_sections=tuple(sections),
            metadata=metadata,
            direct_reply=direct_reply,
        )

class PluginHost:
    def __init__(
        self,
        *,
        registry: PluginRegistry,
        enabled_ids: set[str] | frozenset[str],
        data_root: Path,
        services: Mapping[str, object] | None = None,
        stop_timeout_seconds: float = 5.0,
        capability_catalog: CapabilityCatalog | None = None,
        initial_generation_id: int = 1,
        generation_transform: Callable[[PluginGeneration], PluginGeneration] | None = None,
    ) -> None:
        self._registry = registry
        self._generation_transform = generation_transform
        self._candidates = registry.resolve(enabled_ids)
        self._data_root = Path(data_root)
        self._services = dict(services or {})
        self._stop_timeout_seconds = stop_timeout_seconds
        self._capability_catalog = capability_catalog
        self._lock = asyncio.Lock()
        self._current: PluginGeneration | None = None
        if initial_generation_id < 1:
            raise ValueError("initial_generation_id must be positive")
        self._next_generation_id = initial_generation_id
        self._lease_counts: dict[int, int] = {}
        self._retired: dict[int, PluginGeneration] = {}
        self._stopped_generation_ids: set[int] = set()
        self._lifecycle_diagnostics: list[PluginDiagnostic] = []

    @property
    def lifecycle_diagnostics(self) -> tuple[PluginDiagnostic, ...]:
        return tuple(self._lifecycle_diagnostics)

    async def start(
        self,
        configs: Mapping[str, Mapping[str, object]] | None = None,
        config_provenance: Mapping[str, Mapping[str, str]] | None = None,
    ) -> PluginGeneration:
        async with self._lock:
            if self._current is not None:
                return self._current
            generation = await self._build_generation(
                configs or {}, config_provenance or {}
            )
            self._current = generation
            return generation

    def snapshot(self) -> PluginGeneration:
        if self._current is None:
            raise RuntimeError("plugin host is not started")
        return self._current

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[PluginGeneration]:
        async with self._lock:
            generation = self.snapshot()
            self._lease_counts[generation.id] = self._lease_counts.get(generation.id, 0) + 1
        try:
            yield generation
        finally:
            to_stop: PluginGeneration | None = None
            async with self._lock:
                remaining = self._lease_counts.get(generation.id, 1) - 1
                if remaining:
                    self._lease_counts[generation.id] = remaining
                else:
                    self._lease_counts.pop(generation.id, None)
                    to_stop = self._retired.pop(generation.id, None)
            if to_stop is not None:
                await self._stop_generation(to_stop)

    async def reconfigure(
        self,
        configs: Mapping[str, Mapping[str, object]],
        *,
        minimum_generation_id: int = 1,
        capability_catalog: CapabilityCatalog | None = None,
    ) -> tuple[PluginDiagnostic, ...]:
        to_stop: PluginGeneration | None = None
        async with self._lock:
            old = self.snapshot()
            self._next_generation_id = max(self._next_generation_id, minimum_generation_id)
            try:
                candidate = await self._build_generation(
                    configs, capability_catalog=capability_catalog
                )
            except Exception as exc:
                return (
                    PluginDiagnostic(
                        code="generation_build_failed",
                        message=failure_message(exc),
                    ),
                )
            mcp_plugin_ids = {
                candidate.manifest.id
                for candidate in self._candidates
                if candidate.source.startswith("mcp:")
            }
            failures = tuple(
                diagnostic
                for plugin_id, status in candidate.statuses.items()
                if plugin_id not in mcp_plugin_ids
                if status.state is PluginState.FAILED
                for diagnostic in status.diagnostics
            )
            if failures:
                to_stop = candidate
            else:
                self._current = candidate
                self._capability_catalog = candidate.capability_catalog
                self._retired[old.id] = old
                if self._lease_counts.get(old.id, 0) == 0:
                    to_stop = self._retired.pop(old.id)
        if to_stop is not None:
            await self._stop_generation(to_stop)
        return failures

    async def stop(self) -> None:
        generations: list[PluginGeneration] = []
        async with self._lock:
            if self._current is not None:
                current = self._current
                self._current = None
                if self._lease_counts.get(current.id, 0):
                    self._retired[current.id] = current
                else:
                    generations.append(current)
            for generation_id, generation in tuple(self._retired.items()):
                if self._lease_counts.get(generation_id, 0) == 0:
                    generations.append(generation)
                    self._retired.pop(generation_id)
        for generation in generations:
            await self._stop_generation(generation)

    async def _build_generation(
        self,
        configs: Mapping[str, Mapping[str, object]],
        config_provenance: Mapping[str, Mapping[str, str]] | None = None,
        *,
        capability_catalog: CapabilityCatalog | None = None,
    ) -> PluginGeneration:
        runtimes: dict[str, PluginRuntime] = {}
        statuses: dict[str, PluginStatus] = {}
        diagnostics: list[PluginDiagnostic] = []
        started: list[str] = []
        generation_services: dict[str, object] = {}
        generation_service_owners: dict[str, str] = {}
        generation_events: dict[str, list[EventHandler]] = {}

        for candidate in self._candidates:
            plugin_id = candidate.manifest.id
            failed_dependencies = [
                dependency.plugin_id
                for dependency in candidate.manifest.dependencies
                if statuses[dependency.plugin_id].state is not PluginState.RUNNING
            ]
            if failed_dependencies:
                diagnostic = PluginDiagnostic(
                    code="dependency_unavailable",
                    plugin_id=plugin_id,
                    message=(
                        "dependencies did not start: " + ", ".join(failed_dependencies)
                    ),
                )
                diagnostics.append(diagnostic)
                statuses[plugin_id] = PluginStatus(
                    plugin_id=plugin_id,
                    state=PluginState.FAILED,
                    health=PluginHealth.unhealthy(diagnostic.message),
                    diagnostics=(diagnostic,),
                )
                continue

            try:
                runtime = await self._start_candidate(
                    candidate,
                    configs.get(plugin_id, {}),
                    generation_services=generation_services,
                    generation_service_owners=generation_service_owners,
                    generation_events=generation_events,
                    config_provenance=(config_provenance or {}).get(plugin_id, {}),
                )
            except Exception as exc:
                code = "config_invalid" if isinstance(exc, PluginConfigError) else "plugin_start_failed"
                diagnostic = PluginDiagnostic(
                    code=code,
                    plugin_id=plugin_id,
                    message=failure_message(exc),
                )
                diagnostics.append(diagnostic)
                statuses[plugin_id] = PluginStatus(
                    plugin_id=plugin_id,
                    state=PluginState.FAILED,
                    health=PluginHealth.unhealthy(failure_message(exc)),
                    diagnostics=(diagnostic,),
                )
                continue

            try:
                health = await runtime.instance.health()
            except Exception as exc:
                try:
                    await self._stop_runtime(runtime)
                except BaseException as cleanup_exc:
                    self._record_cleanup_failure(plugin_id, cleanup_exc)
                diagnostic = PluginDiagnostic(
                    code="plugin_health_failed",
                    plugin_id=plugin_id,
                    message=failure_message(exc),
                )
                diagnostics.append(diagnostic)
                statuses[plugin_id] = PluginStatus(
                    plugin_id=plugin_id,
                    state=PluginState.FAILED,
                    health=PluginHealth.unhealthy(failure_message(exc)),
                    diagnostics=(diagnostic,),
                )
                continue
            if health.status not in {"healthy", "degraded", "preparing"}:
                cleanup_diagnostics: list[PluginDiagnostic] = []
                try:
                    await self._stop_runtime(runtime)
                except BaseException as cleanup_exc:
                    cleanup_diagnostics.append(
                        self._record_cleanup_failure(plugin_id, cleanup_exc)
                    )
                diagnostic = PluginDiagnostic(
                    code="plugin_unhealthy",
                    plugin_id=plugin_id,
                    message=health.message or "plugin reported unhealthy status",
                )
                plugin_diagnostics = (diagnostic, *cleanup_diagnostics)
                diagnostics.extend(plugin_diagnostics)
                statuses[plugin_id] = PluginStatus(
                    plugin_id=plugin_id,
                    state=PluginState.FAILED,
                    health=health,
                    diagnostics=plugin_diagnostics,
                )
                continue
            runtimes[plugin_id] = runtime
            started.append(plugin_id)
            state = (
                PluginState.DEGRADED
                if health.status == "degraded"
                else PluginState.RUNNING
            )
            statuses[plugin_id] = PluginStatus(
                plugin_id=plugin_id,
                state=state,
                health=health,
            )

        try:
            generation = PluginGeneration(
                id=self._next_generation_id,
                plugins=runtimes,
                statuses=statuses,
                diagnostics=tuple(diagnostics),
                capability_catalog=(
                    capability_catalog if capability_catalog is not None
                    else self._capability_catalog
                ),
                _start_order=tuple(started),
            )
            if self._generation_transform is not None:
                generation = self._generation_transform(generation)
        except BaseException:
            for plugin_id in reversed(started):
                try:
                    await self._stop_runtime(runtimes[plugin_id])
                except BaseException:
                    pass
            raise
        self._next_generation_id += 1
        return generation

    async def _start_candidate(
        self,
        candidate: PluginCandidate,
        raw_config: Mapping[str, object],
        *,
        generation_services: dict[str, object],
        generation_service_owners: dict[str, str],
        generation_events: dict[str, list[EventHandler]],
        config_provenance: Mapping[str, str],
    ) -> PluginRuntime:
        manifest = candidate.manifest
        config = manifest.validate_config(raw_config)
        data_dir = self._data_root / manifest.id / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        base_services = dict(self._services)
        scope = PluginScope(
            manifest.id,
            shared_services=generation_services,
            shared_service_owners=generation_service_owners,
            allowed_service_owners=frozenset(
                dependency.plugin_id for dependency in manifest.dependencies
            ),
            base_services=base_services,
            shared_events=generation_events,
        )
        context = PluginContext(
            plugin_id=manifest.id,
            data_dir=data_dir,
            config=config,
            scope=scope,
        )
        try:
            instance_or_awaitable = candidate.factory(context)
            instance = (
                await instance_or_awaitable
                if inspect.isawaitable(instance_or_awaitable)
                else instance_or_awaitable
            )
            if instance.manifest.id != manifest.id:
                raise ValueError(
                    f"factory returned plugin {instance.manifest.id}, expected {manifest.id}"
                )
            setup_result = await instance.setup(context)
            if setup_result is not None:
                raise TypeError(
                    "plugin setup must register contributions on context.scope and return None"
                )
            scope.seal()
            await instance.start()
        except BaseException as primary:
            cleanup_errors: list[BaseException] = []
            try:
                if "instance" in locals():
                    await instance.stop()
            except BaseException as exc:
                cleanup_errors.append(exc)
            cleanup_errors.extend(await scope.dispose())
            if cleanup_errors:
                raise primary from BaseExceptionGroup(
                    f"plugin {manifest.id} startup cleanup failed",
                    cleanup_errors,
                )
            raise
        return PluginRuntime(
            plugin_id=manifest.id,
            instance=instance,
            config=config,
            contributions=scope.snapshot(),
            config_provenance=MappingProxyType(dict(config_provenance)),
            scope=scope,
        )

    async def _stop_generation(self, generation: PluginGeneration) -> None:
        if generation.id in self._stopped_generation_ids:
            return
        self._stopped_generation_ids.add(generation.id)
        for plugin_id in reversed(generation._start_order):
            runtime = generation.plugins.get(plugin_id)
            if runtime is None:
                continue
            try:
                await asyncio.wait_for(
                    self._stop_runtime(runtime),
                    timeout=self._stop_timeout_seconds,
                )
            except BaseException as exc:
                self._record_cleanup_failure(plugin_id, exc)
                continue

    def _record_cleanup_failure(
        self, plugin_id: str, error: BaseException
    ) -> PluginDiagnostic:
        diagnostic = PluginDiagnostic(
            code="plugin_cleanup_failed",
            plugin_id=plugin_id,
            message=failure_message(error),
        )
        self._lifecycle_diagnostics.append(diagnostic)
        logger.error("plugin cleanup failed plugin=%s error=%s", plugin_id, error)
        return diagnostic

    @staticmethod
    async def _stop_runtime(runtime: PluginRuntime) -> None:
        stop_error: BaseException | None = None
        try:
            await runtime.instance.stop()
        except BaseException as exc:
            stop_error = exc
        scope_errors = await runtime.scope.dispose() if runtime.scope is not None else ()
        if stop_error is not None:
            raise stop_error
        if scope_errors:
            raise ExceptionGroup(
                f"plugin {runtime.plugin_id} cleanup failed",
                list(scope_errors),
            )


__all__ = [
    "ContributionConflictError",
    "PluginGeneration",
    "PluginHost",
    "PluginRuntime",
    "PluginStatus",
]
