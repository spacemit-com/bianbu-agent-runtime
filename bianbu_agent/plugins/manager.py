from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from bianbu_agent._internal.locking import CapabilityFilesystemLock
from bianbu_agent.inventory import (
    CapabilityCatalogBuilder,
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InventorySnapshot,
    InstalledCapability,
)
from bianbu_agent.plugins.discovery import DirectoryPluginDiscovery
from bianbu_agent.plugins.host import PluginGeneration, PluginHost
from bianbu_agent.plugins.models import (
    failure_message,
    PluginDiagnostic,
    PluginHealth,
    PluginState,
)
from bianbu_agent.plugins.registry import PluginCandidate, PluginRegistry
from bianbu_agent.plugins.specs import PluginSpec
from bianbu_agent.plugins.composition import (
    compose_plugin_config,
    load_plugin_overlays,
)
from bianbu_agent.skills import DirectorySkillLoader
from bianbu_agent.skills.versions import SkillVersions
from bianbu_agent.mcp.plugin import MCPAuthorizer


SnapshotProvider = Callable[[], InventorySnapshot | Awaitable[InventorySnapshot]]

# Run admission verifies the installed capabilities and opens its Generation
# lease immediately afterwards.  Within that window the verified snapshot is
# still exactly the filesystem state the lease has to decide on, so the lease
# may reuse it instead of walking and hashing every capability a second time.
# Anything older is ignored: an out-of-band install after the handoff is then
# picked up by the next Run at the latest.
VERIFIED_SNAPSHOT_HANDOFF_SECONDS = 5.0

# 文件型 Skill 的看门狗轮询。一次扫描要 walk 一遍 Skill 根目录（K3 实测
# 0.10–0.24s），固定 1s 轮询在 Worker 空转时就吃掉 10–25% 的单核，还要和
# 模型 prefill 抢 CPU。每个 Run 的准入本来就会重新校验能力清单，看门狗只负责
# 让空闲期的目录保持新鲜，所以没变化时逐次退避到上限，一旦有变化立刻回到最小间隔。
SKILL_WATCH_MIN_SECONDS = 1.0
SKILL_WATCH_MAX_SECONDS = 10.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _VerifiedCapabilities:
    """Inventories a Run admission already walked, hashed, and verified."""

    snapshot: InventorySnapshot
    skills: InventorySnapshot | None
    taken_at: float


GenerationRevisionProvider = Callable[[], object]


class CapabilityReloadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        diagnostics: tuple[PluginDiagnostic, ...] = (),
    ) -> None:
        self.diagnostics = diagnostics
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PluginReloadResult:
    previous_generation_id: int
    generation_id: int
    inventory_revision: int
    changed: bool
    diagnostics: tuple[PluginDiagnostic, ...] = ()


class PluginManager:
    def __init__(
        self,
        *,
        snapshot_provider: SnapshotProvider | None = None,
        skill_snapshot_provider: SnapshotProvider | None = None,
        inventory: object | None = None,
        workspace_root: Path,
        plugin_data_root: Path,
        explicit_candidates: Sequence[PluginCandidate] = (),
        plugin_specs: Sequence[PluginSpec] = (),
        services: Mapping[str, object] | None = None,
        stop_timeout_seconds: float = 5.0,
        discover_environment: bool = False,
        generation_revision_provider: GenerationRevisionProvider | None = None,
        mcp_authorizer: MCPAuthorizer | None = None,
        runtime_plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        home_overlay_path: Path | None = None,
    ) -> None:
        if snapshot_provider is not None and inventory is not None:
            raise ValueError("provide snapshot_provider or inventory, not both")
        provider = snapshot_provider or getattr(inventory, "snapshot", None)
        if not callable(provider):
            raise TypeError("a callable snapshot provider is required")
        self._snapshot_provider = provider
        self._skill_snapshot_provider = skill_snapshot_provider
        self._skill_generation: PluginGeneration | None = None
        self._skill_signature: tuple[object, ...] = ()
        self._skill_watch_task: asyncio.Task | None = None
        self._skill_refresh_error: str | None = None
        self._handoff: _VerifiedCapabilities | None = None
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._home_root = self._workspace_root.parent
        self._plugin_data_root = Path(plugin_data_root).expanduser().resolve()
        self._skill_versions = SkillVersions(self._plugin_data_root / ".skill-versions")
        self._skill_leases: dict[int, int] = {}
        self._explicit_candidates = tuple(explicit_candidates)
        specs = tuple(plugin_specs)
        if len({spec.id for spec in specs}) != len(specs):
            raise ValueError("PluginSpec IDs must be unique")
        self._plugin_specs = {spec.id: spec for spec in specs}
        self._runtime_plugin_configs = {
            str(plugin_id): dict(config)
            for plugin_id, config in (runtime_plugin_configs or {}).items()
        }
        self._home_overlay_path = Path(
            home_overlay_path
            if home_overlay_path is not None
            else self._plugin_data_root.parent.parent / "plugins.yaml"
        ).expanduser().resolve()
        self._services = dict(services or {})
        self._stop_timeout_seconds = stop_timeout_seconds
        self._discover_environment = bool(discover_environment)
        # Feature-gated plugins: run.sh enables optional features via --with.
        # BIANBU_DISABLED_PLUGINS lists plugin ids that must not load even when
        # discovered on disk, so an unselected feature (e.g. security) never
        # runs its hooks. Keeps agent.yaml's '*' wildcard following the active
        # feature set instead of hardcoding hook ids per agent.
        self._env_disabled_plugins = frozenset(
            item.strip()
            for item in os.environ.get("BIANBU_DISABLED_PLUGINS", "").split(",")
            if item.strip()
        )
        self._generation_revision_provider = generation_revision_provider or (lambda: 0)
        self._discovery = DirectoryPluginDiscovery(
            workspace_root=self._workspace_root,
            mcp_authorizer=mcp_authorizer,
        )
        self._lock = asyncio.Lock()
        self._host: PluginHost | None = None
        self._inventory_revision = -1
        self._inventory_signature: tuple[object, ...] = ()
        self._executable_signature: tuple[object, ...] = ()
        self._next_generation_id = 1
        self._generation_participants: list[object] = []
        self._prepared_generations: dict[int, tuple[tuple[object, object], ...]] = {}
        self._generation_candidate_active = False
        self._poisoned = False

    @property
    def host(self) -> PluginHost:
        self._ensure_available()
        if self._host is None:
            raise RuntimeError("PluginManager is not started")
        return self._host

    @property
    def current_generation(self) -> PluginGeneration:
        self._ensure_available()
        return self._skill_generation or self.host.snapshot()

    def include_generation_revision(
        self,
        provider: GenerationRevisionProvider,
    ) -> None:
        """Include another immutable-input revision in future reload checks."""

        if not callable(provider):
            raise TypeError("generation revision provider must be callable")
        previous = self._generation_revision_provider
        self._generation_revision_provider = lambda: (previous(), provider())

    def include_generation_participant(self, participant: object) -> None:
        """Join candidate preparation and the atomic Generation publication."""

        required = (
            "begin_generation_candidate",
            "prepare_generation_candidate",
            "activate_generation_candidate",
            "rollback_generation_candidate",
            "abort_generation_candidate",
        )
        if not all(callable(getattr(participant, name, None)) for name in required):
            raise TypeError("generation participant does not implement the lifecycle")
        if all(existing is not participant for existing in self._generation_participants):
            self._generation_participants.append(participant)

    async def health(self):
        """Return fresh health for every plugin in the active Generation."""

        generation = self.current_generation
        results = await asyncio.gather(
            *(runtime.instance.health() for runtime in generation.plugins.values()),
            return_exceptions=True,
        )
        return {
            plugin_id: (
                result
                if isinstance(result, PluginHealth)
                else PluginHealth.unhealthy(failure_message(result))
            )
            for plugin_id, result in zip(generation.plugins, results, strict=True)
        }

    async def reconfigure(
        self,
        configs: Mapping[str, Mapping[str, object]],
    ) -> tuple[PluginDiagnostic, ...]:
        self._ensure_available()
        if self._generation_participants:
            raise RuntimeError(
                "Plugin reconfigure cannot publish a Generation while managed "
                "Generation participants are installed; use reload_installed"
            )
        async with self._lock:
            previous = self.host.snapshot()
            diagnostics = await self.host.reconfigure(
                configs, minimum_generation_id=self._next_generation_id,
                capability_catalog=self.current_generation.capability_catalog,
            )
            if self.host.snapshot() is not previous:
                self._skill_generation = None
                self._next_generation_id = self.host.snapshot().id + 1
        await self.refresh_skills()
        self._collect_skill_versions()
        return diagnostics

    async def start(self) -> PluginGeneration:
        self._ensure_available()
        async with self._lock:
            self._ensure_available()
            if self._host is not None:
                return self._host.snapshot()
            async with CapabilityFilesystemLock(
                self._plugin_data_root.parent, exclusive=False
            ):
                snapshot = await self._snapshot()
                host, generation = await self._build_candidate_isolated(snapshot)
                try:
                    self._activate_prepared_generation(generation)
                except BaseException:
                    await _stop_hosts_best_effort(host)
                    if self._poisoned:
                        self._host = None
                        raise _manager_unavailable_error() from None
                    raise
            self._host = host
            self._inventory_revision = snapshot.revision
            self._inventory_signature = self._generation_signature(snapshot)
            self._executable_signature = self._executable_signature_of(snapshot)
            self._skill_signature = self._skills_signature(snapshot)
            if self._skill_snapshot_provider is not None:
                self._skill_watch_task = asyncio.create_task(
                    self._watch_skills(), name="runtime-skill-watch"
                )
            return generation

    async def close(self) -> None:
        task, self._skill_watch_task = self._skill_watch_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Skill watcher stopped unexpectedly")
        async with self._lock:
            host = self._host
            self._host = None
            self._skill_generation = None
        if host is not None:
            await host.stop()
        self._collect_skill_versions()

    def _collect_skill_versions(self) -> None:
        if self._generation_candidate_active:
            return
        live = set(self._skill_leases)
        if self._host is not None:
            live.add(self.current_generation.id)
        self._skill_versions.collect(live)

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[PluginGeneration]:
        await self.refresh_capabilities()
        async with self._lock:
            host = self.host
            lease = host.lease()
            await lease.__aenter__()
            generation = self.current_generation
            self._skill_leases[generation.id] = self._skill_leases.get(generation.id, 0) + 1
        try:
            yield generation
        finally:
            try:
                await lease.__aexit__(None, None, None)
            finally:
                self._skill_leases[generation.id] -= 1
                if not self._skill_leases[generation.id]:
                    del self._skill_leases[generation.id]
                self._collect_skill_versions()

    async def refresh_capabilities(self) -> None:
        """Adopt Powers installed since the last run before leasing a Generation.

        Skills arrive through their own watcher, but MCP servers and Plugins
        only become visible after the file-backed host is rebuilt.  A running
        Agent installs them through APM for itself, so the next run has to pick
        them up without an operator restart.  Rebuilding replaces the Plugin
        host, which is why this waits until no other run holds a lease; the run
        that is waiting for the lease is not registered yet.
        """

        handoff = self._take_handoff()
        await self.refresh_skills(snapshot=None if handoff is None else handoff.skills)
        if self._skill_leases or self._host is None:
            return
        if handoff is None:
            snapshot = await self._snapshot()
        else:
            snapshot = handoff.snapshot
        if self._executable_signature_of(snapshot) == self._executable_signature:
            return
        try:
            await self.reload_installed()
        except CapabilityReloadError as exc:
            # A half-written or rejected package must not take down every chat:
            # keep the Generation this Worker already proved, and let the next
            # run retry the rebuild.
            logger.warning("Keeping previous capability generation: %s", exc)

    @staticmethod
    def _executable_signature_of(snapshot: InventorySnapshot) -> tuple[object, ...]:
        """Signature of the Powers that need the file-backed host rebuilt.

        Skills publish through their own overlay and must never restart the
        host under a live lease, so they are excluded here.
        """

        return _snapshot_signature(InventorySnapshot(
            revision=0,
            items=tuple(
                item for item in snapshot.items
                if item.source.kind is not CapabilityKind.SKILL
            ),
        ))

    @staticmethod
    def _skills_signature(snapshot: InventorySnapshot) -> tuple[object, ...]:
        return _snapshot_signature(InventorySnapshot(
            revision=0,
            items=tuple(item for item in snapshot.items
                        if item.source.kind is CapabilityKind.SKILL),
        ))

    async def _watch_skills(self) -> None:
        delay = SKILL_WATCH_MIN_SECONDS
        while True:
            await asyncio.sleep(delay)
            changed = await self.refresh_skills()
            delay = (
                SKILL_WATCH_MIN_SECONDS
                if changed
                else min(delay * 2, SKILL_WATCH_MAX_SECONDS)
            )

    async def refresh_skills(self, *, snapshot: InventorySnapshot | None = None) -> bool:
        """Publish file-only Skills without restarting the owning PluginHost.

        A lease still belongs to the host that owns its executable resources;
        it captures the current immutable catalog overlay for that run.

        ``snapshot`` is an already-verified Inventory (see
        ``VERIFIED_SNAPSHOT_HANDOFF_SECONDS``); when it is given the Skill scan
        is not repeated.

        Returns whether a new Skill generation was published, which the watcher
        uses to decide between the fast and the backed-off poll interval.
        """
        if self._skill_snapshot_provider is None and snapshot is None:
            return False
        verified = snapshot is not None
        async with self._lock:
            if self._host is None:
                return False
            try:
                async with CapabilityFilesystemLock(
                    self._plugin_data_root.parent, exclusive=False
                ):
                    if snapshot is None:
                        value = self._skill_snapshot_provider()
                        snapshot = await value if inspect.isawaitable(value) else value
                    signature = self._skills_signature(snapshot)
                    if signature == self._skill_signature:
                        self._skill_refresh_error = None
                        return False
                    if not verified:
                        CapabilityCatalogBuilder.verify_snapshot(snapshot)
                    current = self.current_generation
                    catalog = current.capability_catalog
                    if catalog is None:
                        return False
                    skills = {}
                    sources = {key: source for key, source in catalog.sources.items()
                               if key[0] is not CapabilityKind.SKILL}
                    for item in snapshot.items:
                        if item.source.kind is CapabilityKind.SKILL and item.enabled:
                            skill = DirectorySkillLoader().load(item.source.root)
                            CapabilityCatalogBuilder._add(skills, skill.spec.name, skill, "skill")
                            sources[(CapabilityKind.SKILL, skill.spec.name)] = (
                                CapabilityCatalogBuilder._source_from_skill(skill)
                            )
                    candidate = replace(current, id=self._next_generation_id,
                                        capability_catalog=replace(catalog, skills=skills,
                                                                   sources=sources))
                    candidate = self._skill_versions.freeze(candidate)
                    await self._begin_generation_candidate()
                    prepared = await self._prepare_generation_candidate(candidate)
                    self._prepared_generations[candidate.id] = prepared
                    self._activate_prepared_generation(candidate)
                    self._skill_generation = candidate
                    self._collect_skill_versions()
                    self._next_generation_id = candidate.id + 1
                    self._skill_signature = signature
                    self._skill_refresh_error = None
                    return True
            except BaseException as exc:
                if self._generation_candidate_active:
                    await self._abort_generation_candidate(primary=exc)
                if not isinstance(exc, Exception):
                    raise
                # Editors often expose a partially written manifest. Retain the
                # previous valid generation and retry on the next scan/run.
                if self._poisoned:
                    raise
                message = str(exc)
                if message != self._skill_refresh_error:
                    logger.warning("Keeping previous Skill generation: %s", exc)
                    self._skill_refresh_error = message
                return False

    def _take_handoff(self) -> _VerifiedCapabilities | None:
        """Consume a Run-admission verification for the lease that follows it."""

        handoff, self._handoff = self._handoff, None
        if handoff is None:
            return None
        if time.monotonic() - handoff.taken_at > VERIFIED_SNAPSHOT_HANDOFF_SECONDS:
            return None
        return handoff

    async def reload_installed(
        self, *, force: bool = False, handoff: bool = False
    ) -> PluginReloadResult:
        """Re-verify installed capabilities and rebuild the Generation on change.

        ``handoff`` is set by Run admission, whose Generation lease follows in
        the same turn: the snapshot verified here is then reused instead of
        walked and hashed again (``VERIFIED_SNAPSHOT_HANDOFF_SECONDS``).
        """

        self._ensure_available()
        old_host: PluginHost | None = None
        async with self._lock:
            self._ensure_available()
            # Every reload supersedes an unconsumed handoff: the Run that asked
            # for it is no longer the caller that opens the next lease.
            self._handoff = None
            current = self.current_generation
            try:
                async with CapabilityFilesystemLock(
                    self._plugin_data_root.parent, exclusive=False
                ):
                    if self._skill_snapshot_provider is not None:
                        value = self._skill_snapshot_provider()
                        skills = await value if inspect.isawaitable(value) else value
                        CapabilityCatalogBuilder.verify_snapshot(skills)
                    else:
                        skills = None
                    snapshot = await self._snapshot()
                    CapabilityCatalogBuilder.verify_snapshot(snapshot)
                    if handoff:
                        self._handoff = _VerifiedCapabilities(
                            snapshot=snapshot,
                            skills=skills,
                            taken_at=time.monotonic(),
                        )
                    signature = self._generation_signature(snapshot)
                    candidate_pair = (
                        None
                        if not force and signature == self._inventory_signature
                        else await self._build_candidate_isolated(snapshot)
                    )
            except Exception as exc:
                diagnostic = PluginDiagnostic(
                    code="capability_verification_failed",
                    message=failure_message(exc),
                )
                raise CapabilityReloadError(
                    f"failed to verify installed capabilities: {exc}",
                    diagnostics=(diagnostic,),
                ) from exc
            if candidate_pair is None:
                return PluginReloadResult(
                    previous_generation_id=current.id,
                    generation_id=current.id,
                    inventory_revision=snapshot.revision,
                    changed=False,
                )
            candidate_host, candidate = candidate_pair
            if (
                force
                and signature == self._inventory_signature
                and _mcp_generation_signature(candidate)
                == _mcp_generation_signature(current)
            ):
                self._prepared_generations.pop(candidate.id, None)
                await self._abort_generation_candidate()
                await _stop_hosts_best_effort(candidate_host)
                return PluginReloadResult(
                    previous_generation_id=current.id,
                    generation_id=current.id,
                    inventory_revision=snapshot.revision,
                    changed=False,
                )
            try:
                self._activate_prepared_generation(candidate)
            except BaseException as exc:
                await _stop_hosts_best_effort(candidate_host)
                if self._poisoned:
                    quarantined = self._host
                    self._host = None
                    await _stop_hosts_best_effort(quarantined)
                    raise _manager_unavailable_error() from None
                if isinstance(exc, asyncio.CancelledError):
                    raise
                diagnostic = PluginDiagnostic(
                    code="capability_activation_failed",
                    message=failure_message(exc),
                )
                raise CapabilityReloadError(
                    f"failed to activate installed capabilities: {exc}",
                    diagnostics=(diagnostic,),
                ) from exc
            old_host = self._host
            self._host = candidate_host
            self._skill_generation = None
            self._skill_signature = self._skills_signature(snapshot)
            self._inventory_revision = snapshot.revision
            self._inventory_signature = signature
            self._executable_signature = self._executable_signature_of(snapshot)
            result = PluginReloadResult(
                previous_generation_id=current.id,
                generation_id=candidate.id,
                inventory_revision=snapshot.revision,
                changed=True,
                diagnostics=candidate.diagnostics,
            )
        if old_host is not None:
            await old_host.stop()
        self._collect_skill_versions()
        return result

    async def _build_candidate(
        self,
        snapshot: InventorySnapshot,
    ) -> tuple[PluginHost, PluginGeneration]:
        async with CapabilityFilesystemLock(
            self._plugin_data_root.parent, exclusive=False
        ):
            return await self._build_candidate_isolated(snapshot)

    async def _build_candidate_isolated(
        self,
        snapshot: InventorySnapshot,
    ) -> tuple[PluginHost, PluginGeneration]:
        await self._begin_generation_candidate()
        try:
            return await self._build_candidate_after_begin(snapshot)
        except BaseException as exc:
            if self._generation_candidate_active:
                await self._abort_generation_candidate(primary=exc)
            raise

    async def _build_candidate_after_begin(
        self,
        snapshot: InventorySnapshot,
    ) -> tuple[PluginHost, PluginGeneration]:
        try:
            snapshot = self._apply_plugin_specs(snapshot)
            home_overlays = load_plugin_overlays(self._home_overlay_path)
            disabled_plugins = {
                plugin_id
                for plugin_id, spec in self._plugin_specs.items()
                if not spec.enabled
            } | {
                plugin_id
                for plugin_id, overlay in home_overlays.items()
                if not overlay.enabled
            } | set(self._env_disabled_plugins)
            snapshot = self._apply_parent_enablement(snapshot, disabled_plugins)
            catalog = CapabilityCatalogBuilder(
                workspace_root=self._workspace_root,
                tool_services=self._services,
            ).build(snapshot)
        except BaseException as exc:
            await self._abort_generation_candidate(primary=exc)
        registry = PluginRegistry()
        for candidate in self._explicit_candidates:
            registry.register(
                candidate.factory,
                candidate.manifest,
                source=candidate.source,
            )
        discovered_candidates = self._discovery.discover(snapshot)
        for candidate in discovered_candidates:
            registry.register(
                candidate.factory,
                candidate.manifest,
                source=candidate.source,
            )
        if self._discover_environment:
            diagnostics = registry.discover_entry_points()
            if diagnostics:
                raise CapabilityReloadError(
                    "one or more environment Plugins could not be discovered",
                    diagnostics=diagnostics,
                )
        plugins = tuple(
            item
            for item in snapshot.items
            if item.enabled and item.source.kind is CapabilityKind.PLUGIN
        )
        explicit_ids = {
            candidate.manifest.id for candidate in self._explicit_candidates
        }
        configured_enabled = {
            plugin_id
            for plugin_id, spec in self._plugin_specs.items()
            if spec.enabled
        }
        configured_disabled = {
            plugin_id
            for plugin_id, spec in self._plugin_specs.items()
            if not spec.enabled
        }
        home_enabled = {
            plugin_id for plugin_id, overlay in home_overlays.items() if overlay.enabled
        }
        home_disabled = {
            plugin_id for plugin_id, overlay in home_overlays.items() if not overlay.enabled
        }
        discovered_ids = {candidate.manifest.id for candidate in discovered_candidates}
        enabled_ids = (explicit_ids | configured_enabled | home_enabled | discovered_ids | {
            item.source.capability_id for item in plugins
        }) - home_disabled - configured_disabled - set(self._env_disabled_plugins)
        configs: dict[str, Mapping[str, object]] = {}
        config_provenance: dict[str, Mapping[str, str]] = {}
        candidates_by_id = {
            candidate.manifest.id: candidate for candidate in registry.candidates
        }
        missing_candidates = sorted(enabled_ids - set(candidates_by_id))
        if missing_candidates:
            raise CapabilityReloadError(
                "enabled Plugin is not installed or registered",
                diagnostics=tuple(
                    PluginDiagnostic(
                        code="plugin_not_found",
                        plugin_id=plugin_id,
                        message=f"enabled Plugin is not available: {plugin_id}",
                    )
                    for plugin_id in missing_candidates
                ),
            )
        for plugin_id in sorted(enabled_ids):
            candidate = candidates_by_id[plugin_id]
            overlay = home_overlays.get(plugin_id)
            spec = self._plugin_specs.get(plugin_id)
            effective = compose_plugin_config(
                candidate.manifest,
                runtime_config=self._runtime_plugin_configs.get(plugin_id),
                home_config=(overlay.config if overlay is not None else None),
                spec_config=(spec.config if spec is not None else None),
            )
            configs[plugin_id] = effective.config
            config_provenance[plugin_id] = effective.provenance
        host = PluginHost(
            registry=registry,
            enabled_ids=enabled_ids,
            data_root=self._plugin_data_root,
            services=self._services,
            stop_timeout_seconds=self._stop_timeout_seconds,
            capability_catalog=catalog,
            initial_generation_id=self._next_generation_id,
            generation_transform=self._skill_versions.freeze,
        )
        try:
            generation = await host.start(configs, config_provenance)
        except BaseException as exc:
            await self._abort_generation_candidate(primary=exc)
        mcp_plugin_ids = {
            candidate.manifest.id
            for candidate in discovered_candidates
            if candidate.source.startswith("mcp:")
        }
        failures = tuple(
            diagnostic
            for plugin_id, status in generation.statuses.items()
            if plugin_id not in mcp_plugin_ids
            if status.state is PluginState.FAILED
            for diagnostic in status.diagnostics
        )
        if failures:
            await host.stop()
            failure = CapabilityReloadError(
                "one or more enabled Plugins failed to start",
                diagnostics=failures,
            )
            await self._abort_generation_candidate(primary=failure)
        try:
            prepared = await self._prepare_generation_candidate(generation)
        except BaseException:
            await host.stop()
            raise
        self._prepared_generations[generation.id] = prepared
        self._next_generation_id = generation.id + 1
        return host, generation

    async def _begin_generation_candidate(self) -> None:
        if self._generation_candidate_active:
            raise RuntimeError("Generation candidate lifecycle is already active")
        self._generation_candidate_active = True
        begun: list[object] = []
        try:
            for participant in self._generation_participants:
                begun.append(participant)
                value = participant.begin_generation_candidate()
                if inspect.isawaitable(value):
                    await value
        except BaseException as exc:
            await self._abort_generation_candidate(begun, primary=exc)

    async def _prepare_generation_candidate(
        self,
        generation: PluginGeneration,
    ) -> tuple[tuple[object, object], ...]:
        prepared: list[tuple[object, object]] = []
        try:
            for participant in self._generation_participants:
                value = participant.prepare_generation_candidate(generation)
                token = await value if inspect.isawaitable(value) else value
                prepared.append((participant, token))
        except BaseException as exc:
            await self._abort_generation_candidate(primary=exc)
        return tuple(prepared)

    async def _abort_generation_candidate(
        self,
        participants: Sequence[object] | None = None,
        *,
        primary: BaseException | None = None,
    ) -> None:
        cleanup_errors: list[BaseException] = []
        selected = tuple(
            self._generation_participants if participants is None else participants
        )
        for participant in reversed(selected):
            try:
                value = participant.abort_generation_candidate()
                if inspect.isawaitable(value):
                    await value
            except BaseException as exc:
                cleanup_errors.append(exc)
        self._generation_candidate_active = False
        self._collect_skill_versions()
        if primary is not None:
            _raise_primary_with_cleanup(primary, cleanup_errors)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "Generation candidate cleanup failed",
                cleanup_errors,
            )

    def _activate_prepared_generation(self, generation: PluginGeneration) -> None:
        prepared = self._prepared_generations.get(generation.id, ())
        try:
            for participant, token in prepared:
                value = participant.activate_generation_candidate(token)
                if inspect.isawaitable(value):
                    close = getattr(value, "close", None)
                    if callable(close):
                        close()
                    raise TypeError("Generation activation must be synchronous")
        except BaseException as exc:
            rollback_errors: list[BaseException] = []
            for participant, token in reversed(prepared):
                try:
                    value = participant.rollback_generation_candidate(token)
                    if inspect.isawaitable(value):
                        close = getattr(value, "close", None)
                        if callable(close):
                            close()
                        raise TypeError("Generation rollback must be synchronous")
                except BaseException as rollback_exc:
                    rollback_errors.append(rollback_exc)
            self._prepared_generations.pop(generation.id, None)
            self._generation_candidate_active = False
            if rollback_errors:
                self._poisoned = True
                raise _manager_unavailable_error() from None
            _raise_primary_with_cleanup(exc, rollback_errors)
        self._prepared_generations.pop(generation.id, None)
        self._generation_candidate_active = False

    def _ensure_available(self) -> None:
        if self._poisoned:
            raise _manager_unavailable_error()

    def _apply_plugin_specs(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        items = []
        for item in snapshot.items:
            if item.source.kind is not CapabilityKind.PLUGIN:
                items.append(item)
                continue
            spec = self._plugin_specs.get(item.source.capability_id)
            if spec is None:
                items.append(item)
                continue
            items.append(
                InstalledCapability(
                    source=item.source,
                    enabled=item.enabled and spec.enabled,
                )
            )
        return InventorySnapshot(revision=snapshot.revision, items=tuple(items))

    @staticmethod
    def _apply_parent_enablement(
        snapshot: InventorySnapshot,
        disabled_plugins: set[str],
    ) -> InventorySnapshot:
        if not disabled_plugins:
            return snapshot
        return InventorySnapshot(
            revision=snapshot.revision,
            items=tuple(
                replace(item, enabled=False)
                if (
                    item.source.kind is CapabilityKind.PLUGIN
                    and item.source.capability_id in disabled_plugins
                )
                or (
                    item.source.origin is CapabilityOrigin.PLUGIN
                    and item.source.plugin_id in disabled_plugins
                )
                else item
                for item in snapshot.items
            ),
        )

    async def _snapshot(self) -> InventorySnapshot:
        value = self._snapshot_provider()
        snapshot = await value if inspect.isawaitable(value) else value
        if isinstance(snapshot, InventorySnapshot):
            return snapshot
        try:
            items = tuple(
                InstalledCapability(
                    source=CapabilitySource(
                        kind=CapabilityKind(item.source.kind.value),
                        capability_id=item.source.capability_id,
                        version=item.source.version,
                        root=item.source.root,
                        manifest_path=item.source.manifest_path,
                        content_digest=item.source.content_digest,
                        origin=CapabilityOrigin(item.source.origin.value),
                        plugin_id=item.source.plugin_id,
                        metadata=dict(item.source.metadata),
                    ),
                    enabled=item.enabled,
                )
                for item in snapshot.items
            )
            return InventorySnapshot(revision=int(snapshot.revision), items=items)
        except (AttributeError, TypeError, ValueError) as exc:
            raise TypeError("snapshot provider must return InventorySnapshot") from exc

    def _candidate_signature(self, snapshot: InventorySnapshot) -> tuple[object, ...]:
        from bianbu_agent.agents import AgentPackageManager
        from bianbu_agent.home import AgentHome

        agents = tuple(
            (package.id, package.content_digest)
            for package in AgentPackageManager(AgentHome(self._home_root)).list()
            if (package.root / "tools").exists()
        )
        return (_snapshot_signature(snapshot), agents)

    def _generation_signature(self, snapshot: InventorySnapshot) -> tuple[object, ...]:
        return (
            self._candidate_signature(snapshot),
            self._generation_revision_provider(),
        )


def _snapshot_signature(snapshot: InventorySnapshot) -> tuple[object, ...]:
    return (
        snapshot.revision,
        *(
            (
                item.source.kind.value,
                item.source.capability_id,
                item.source.version,
                str(item.source.root),
                item.source.content_digest,
                item.enabled,
            )
            for item in sorted(
                snapshot.items,
                key=lambda candidate: (
                    candidate.source.kind.value,
                    candidate.source.capability_id,
                ),
            )
        ),
    )


def _mcp_generation_signature(generation: PluginGeneration) -> tuple[object, ...]:
    def frozen(value: object) -> object:
        if isinstance(value, Mapping):
            return tuple(sorted((str(key), frozen(item)) for key, item in value.items()))
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(frozen(item) for item in value)
        return value

    servers = []
    for mcp_id, server in sorted(generation.mcps.items()):
        tools = []
        for tool_id in generation.mcp_tools.get(mcp_id, ()):
            contribution = generation.tools[tool_id]
            spec = contribution.spec
            tools.append((
                tool_id,
                spec.name,
                spec.description,
                frozen(spec.input_schema),
                frozen(spec.metadata),
            ))
        servers.append((
            mcp_id,
            server.display_name,
            server.owner_plugin_id,
            server.source_kind,
            server.transport,
            server.command,
            tuple(tools),
        ))
    return tuple(servers)


def _raise_primary_with_cleanup(
    primary: BaseException,
    cleanup_errors: Sequence[BaseException],
) -> None:
    if cleanup_errors:
        raise primary from BaseExceptionGroup(
            "Generation candidate cleanup failed",
            list(cleanup_errors),
        )
    raise primary


def _manager_unavailable_error() -> RuntimeError:
    return RuntimeError(
        "PluginManager is unavailable after a Generation rollback failure"
    )


async def _stop_hosts_best_effort(*hosts: PluginHost | None) -> None:
    seen: set[int] = set()
    for host in hosts:
        if host is None or id(host) in seen:
            continue
        seen.add(id(host))
        try:
            await host.stop()
        except BaseException:
            # The manager is already fail-closed; cleanup cannot make it usable.
            pass


__all__ = [
    "CapabilityReloadError",
    "PluginManager",
    "PluginReloadResult",
]
