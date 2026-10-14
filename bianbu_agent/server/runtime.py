from __future__ import annotations

import json
import asyncio
import inspect
import logging
import sqlite3
import time
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from bianbu_agent.agents.manager import AgentManager
from bianbu_agent.agents import (
    AgentPackageManager,
    AgentRegistry,
    ManifestAgentStore,
    validate_agent_id,
)
from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent.inventory import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InstalledCapability,
    InventorySnapshot,
)
from bianbu_agent.home import AgentHome
from bianbu_agent.skills import DirectorySkillLoader
from bianbu_agent.skills import SkillPackageManager
from bianbu_agent.tools import DirectoryToolLoader
from bianbu_agent.plugins import (
    PluginPackageManager,
)
from bianbu_agent.plugins.manifest import (
    load_installed_plugin_manifest,
    plugin_definition_path,
)
from bianbu_agent.config import RUNTIME_VERSION, RuntimeConfig
from bianbu_agent.capture import ProviderCaptureCallback, ProviderCaptureStore
from bianbu_agent.context import CompressionSettingsProvider
from bianbu_agent.engines.default import build_default_engine
from bianbu_agent.errors import AgentValidationError, RuntimeStartupError
from bianbu_agent.models import (
    ChatModel,
    YAMLModelResolver,
    YAMLModelStore,
    ModelRegistry,
    ModelResolver,
)
from bianbu_agent.mcp import (
    MCPPackageManager,
    MCPPreparationManager,
    selects_mcp,
    validate_mcp_config,
)
from bianbu_agent.mcp.config import load_mcp_config
from bianbu_agent.plugins import (
    PluginCandidate,
    PluginManager,
    PluginRegistration,
    PluginRuntime,
    PluginSpec,
)
from bianbu_agent.runs.manager import EngineBuilder, RunManager
from bianbu_agent.sessions import JSONSessionStore, SessionManager, SessionStore
from bianbu_agent.sessions.log_store import (
    SessionLogStore,
    read_session_transcript,
)
from bianbu_agent.security.security_runtime import (
    RuntimeStartRequest,
    SecurityRuntimeClient,
    SecurityRuntimeRejected,
    SecurityRuntimeUnavailable,
    current_attestation,
    security_client_module,
)
from bianbu_agent.security.agent_registration import (
    SecurityAgentRegistration,
    agent_framework,
    logical_agent_capabilities,
)
from bianbu_agent.security.execution_runtime import (
    ExecutionCoordinator,
    ExecutionRecord,
    ExecutionRequest,
)
from bianbu_agent.server.memory_registry import MemoryRegistration
from bianbu_agent.server.memory_registry import register as register_memory_runtime
from bianbu_agent.server.memory_registry import unregister as unregister_memory_runtime
from bianbu_agent.observability import RuntimeLogStore


logger = logging.getLogger(__name__)

PROVIDER_CAPTURE_MAINTENANCE_SECONDS = 15 * 60


def _is_unmanaged_worker_fallback() -> bool:
    """Return whether this Worker is the explicit development fallback.

    The marker is injected by ``WorkerSupervisor`` after the managed launch
    path has failed.  Require the same development opt-in and synthetic lease
    here so an arbitrary environment variable cannot disable Runtime startup
    checks in a production Worker.
    """
    return (
        os.environ.get("BIANBU_WORKER_UNMANAGED_FALLBACK") == "1"
        and os.environ.get("BIANBU_WORKER_MANAGED") != "1"
        and os.environ.get("BIANBU_WORKER_LEASE_ID", "").strip() == "dev"
        and os.environ.get("BIANBU_RUNTIME_MODE", "development").strip().lower()
        == "development"
        and os.environ.get("BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER") == "1"
    )


def _contributed_capability_ids(runtime: PluginRuntime) -> tuple[str, ...]:
    """Capabilities one running Plugin contributes, for diagnostics output."""

    contributions = runtime.contributions
    names = [
        str(item.spec.metadata.get("qualified_id") or item.spec.name)
        for item in contributions.tools
    ]
    names.extend(
        str(item.spec.metadata.get("qualified_id") or item.spec.name)
        for item in contributions.skills
    )
    names.extend(
        str(item.metadata.get("qualified_id") or f"{runtime.plugin_id}/{item.id}")
        for item in contributions.hooks
    )
    if contributions.mcp_runtime is not None:
        names.append(f"{runtime.plugin_id}/mcp")
    return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class RuntimeDiagnostics:
    started: bool
    closing: bool
    generation_id: int | None
    inventory_revision: int | None
    active_runs: int
    waiting_runs: int = 0
    # (plugin_id, lifecycle state, health status, contributed capability ids)
    plugin_statuses: tuple[tuple[str, str, str, tuple[str, ...]], ...] = ()
    recent_errors: tuple[str, ...] = ()
    security_pending: tuple[tuple[str, str], ...] = ()


async def _collect_async_error(
    errors: list[Exception],
    cancellations: list[asyncio.CancelledError],
    action,
) -> None:
    try:
        await action()
    except asyncio.CancelledError as exc:
        cancellations.append(exc)
    except Exception as exc:
        errors.append(exc)


class BianbuAgentRuntime:
    def __init__(
        self,
        *,
        config: RuntimeConfig,
        default_model: ChatModel | None,
        agent_id: str | None = None,
        models: Mapping[str, ChatModel] | None = None,
        plugins: Sequence[PluginSpec] = (),
        plugin_registrations: Sequence[PluginRegistration] = (),
        model_resolver: ModelResolver | None = None,
        agent_registry: AgentRegistry | None = None,
        session_store: SessionStore | None = None,
        run_store: object | None = None,
        plugin_manager: PluginManager | None = None,
        plugin_services: Mapping[str, object] | None = None,
        managed_plugin_services: Mapping[str, object] | None = None,
        engine_builder: EngineBuilder | None = None,
        role: str = "worker",
    ) -> None:
        self.config = config
        from bianbu_agent.server.metrics import RuntimeMetrics
        self.metrics = RuntimeMetrics()
        role = str(role).strip().lower()
        if role not in {"control", "worker"}:
            raise ValueError("Runtime role must be control or worker")
        self.role = role
        self._control_plugin_runtime = None
        self.home = AgentHome(config.data_dir).prepare()
        self.provider_capture_store = ProviderCaptureStore(
            self.home.logs_dir / "provider-capture"
        )
        self.runtime_log_store = RuntimeLogStore(
            self.home.logs_dir / "operational" / "runtime.jsonl"
        )
        self.provider_capture_callback = ProviderCaptureCallback(
            self.provider_capture_store
        )
        self.skills = SkillPackageManager(self.home)
        # Package installation is separate from the executable Plugin manager.
        # This manager owns the installed package lifecycle.
        self.plugins = PluginPackageManager(self.home)
        self.mcps = MCPPackageManager(self.home)
        self.model_configs = YAMLModelStore(
            self.home.models_dir,
            capability_change_handler=self._refresh_model_generation,
        )
        self.default_model = default_model
        self._worker_agent_id = (
            validate_agent_id(agent_id) if agent_id is not None else None
        )
        # Cross-store catalog decisions (Agent references versus Provider and
        # executable package mutations) must be serialized as one decision.
        self.catalog_mutation_lock = asyncio.Lock()
        self._models = ModelRegistry(
            default_model=default_model,
            models=models,
            resolver=model_resolver or YAMLModelResolver(self.home.models_dir),
        )
        self.agent_packages = AgentPackageManager(self.home)
        self._agent_store = agent_registry or ManifestAgentStore(
            self.home,
            self.agent_packages,
        )
        self._session_store = session_store or JSONSessionStore(self.home.sessions_dir)
        self._run_store = run_store or SessionLogStore(self.home.sessions_dir)
        if plugin_manager is not None and (plugins or plugin_registrations):
            raise ValueError(
                "plugins/plugin_registrations cannot be combined with plugin_manager"
            )
        if plugin_manager is not None and (
            plugin_services or managed_plugin_services
        ):
            raise ValueError(
                "plugin_services/managed_plugin_services cannot be combined "
                "with plugin_manager"
            )
        candidates = tuple(
            PluginCandidate(
                factory=registration.factory,
                manifest=registration.manifest,
                source=registration.source,
            )
            for registration in plugin_registrations
        )
        host_services = dict(plugin_services or {})
        managed_services = dict(managed_plugin_services or {})
        overlap = host_services.keys() & managed_services.keys()
        if overlap:
            raise ValueError(
                "plugin service names must be unique: "
                + ", ".join(sorted(overlap))
            )
        host_services.update(managed_services)
        self._managed_plugin_services = tuple(
            {id(service): service for service in managed_services.values()}.values()
        )
        # The control plane is intentionally metadata-only. Do not bind/start
        # ToolRuntimeServices there; the Worker owns the model/tool/plugin
        # execution graph.
        if role == "control":
            self._managed_plugin_services = ()
        # Starting an MCP server can be slow the first time (an ``npx``
        # declaration downloads its package) and never belongs to a caller:
        # the install only registers, and this manager brings every declared
        # server up in the background on each Worker start.
        self.mcp_preparation: MCPPreparationManager | None = None
        if role == "worker" and plugin_manager is None:
            self.mcp_preparation = MCPPreparationManager(
                self.home,
                reload=self._reload_after_mcp_preparation,
            )
            host_services[MCPPreparationManager.service_name] = self.mcp_preparation
            self._managed_plugin_services = (
                *self._managed_plugin_services,
                self.mcp_preparation,
            )
        self._active_plugin_services: list[object] = []
        if plugin_manager is None:
            async def authorize_mcp(agent_id: str, mcp_id: str) -> bool:
                from bianbu_agent.agents.selection import task_agent
                leased_agent = task_agent.get()
                if leased_agent is not None:
                    if leased_agent.id != agent_id or not selects_mcp(
                        leased_agent.mcps, mcp_id
                    ):
                        return False
                else:
                    manager = getattr(self, "agents", None)
                    if manager is None:
                        return False
                    try:
                        agent = await manager.get(agent_id)
                    except (KeyError, AgentValidationError):
                        return False
                    if not _agent_selects_mcp(agent, mcp_id):
                        return False
                # bianbu-security 层 1：整台 MCP server 授权（INT-RT-08）。
                # 层 2（单工具粒度 + DLP 扫参数）在 before_tool hook 里做。
                return await _securityd_allows_mcp(agent_id, mcp_id)

            self._plugin_manager = PluginManager(
                snapshot_provider=self._execution_inventory_snapshot,
                skill_snapshot_provider=lambda: _inventory_snapshot(
                    config, skills_only=True, strict_skills=True
                ),
                workspace_root=config.data_dir / "workspace",
                plugin_data_root=config.data_dir / "workspace" / "plugin-data",
                stop_timeout_seconds=config.plugin_stop_timeout_seconds,
                discover_environment=True,
                explicit_candidates=candidates,
                plugin_specs=plugins,
                services=host_services,
                generation_revision_provider=(
                    lambda: self.model_configs.capability_revision
                ),
                mcp_authorizer=authorize_mcp,
                runtime_plugin_configs=config.plugin_configs,
                home_overlay_path=config.data_dir / "plugins.yaml",
            )
        else:
            self._plugin_manager = plugin_manager
            include_revision = getattr(
                self._plugin_manager,
                "include_generation_revision",
                None,
            )
            if callable(include_revision):
                include_revision(lambda: self.model_configs.capability_revision)
        include_participant = getattr(
            self._plugin_manager,
            "include_generation_participant",
            None,
        )
        if callable(include_participant):
            for service in self._managed_plugin_services:
                if callable(getattr(service, "prepare_generation_candidate", None)):
                    include_participant(service)
        self._configured_engine_builder = engine_builder
        self._engine_builder = engine_builder
        self._started = False
        self._closing = False
        self._recent_errors: list[str] = []
        # Logical Agent registration is deliberately independent from the
        # process lease.  Keep failures observable in development while
        # allowing conversations to continue when securityd is unavailable.
        self._security_pending: dict[str, str] = {}
        self._opened: list[object] = []
        self._provider_capture_maintenance_task: asyncio.Task[None] | None = None
        self._provider_capture_maintenance_stop: asyncio.Event | None = None
        # Per-Agent execution coordinator.  The legacy process-level
        # Prepare/Commit below remains as a compatibility proof for the Runtime
        # service itself; new managed Agent instances use this coordinator.
        self.execution_coordinator = ExecutionCoordinator(
            state_dir=(self.home.root / "runtime" / "worker"
                       if self.role == "worker" else self.home.root / "runtime"),
        )
        self._security_runtime_client: SecurityRuntimeClient | None = None
        self._security_runtime_decision = None
        self._security_runtime_instance_id = ""
        self._memory_registration_path: Path | None = None
        self._memory_registration: MemoryRegistration | None = None
        self._memory_maintenance_stop = asyncio.Event()
        self._memory_maintenance_task: asyncio.Task | None = None
        self.agents: AgentManager
        self.sessions: SessionManager
        self.runs: RunManager

    async def _execution_inventory_snapshot(self) -> InventorySnapshot:
        snapshot = _inventory_snapshot(self.config)
        if self.role != "worker" or self._worker_agent_id is None:
            return snapshot
        agent = await self._agent_store.get(self._worker_agent_id)
        if agent is None:
            raise KeyError(self._worker_agent_id)
        def enabled(item):
            source = item.source
            if source.kind is CapabilityKind.PLUGIN:
                return "*" in agent.plugins or source.capability_id in agent.plugins
            if source.kind is CapabilityKind.MCP:
                if source.plugin_id and agent.plugins is not None:
                    return "*" in agent.plugins or source.plugin_id in agent.plugins
                # The Agent may name the server by its APM name (``nws``) while
                # the package this device installed is ``mcp.nws``.
                return selects_mcp(agent.mcps, source.capability_id)
            return True

        return replace(snapshot, items=tuple(
            replace(item, enabled=item.enabled and enabled(item))
            for item in snapshot.items
        ))

    async def _refresh_model_generation(self) -> None:
        """Reload plugin generation when model capabilities change.

        Only reloads if both the runtime and plugin manager are fully started.
        During startup, the model_configs store may trigger capability changes
        before the plugin manager is ready.
        """
        if not self._started:
            return
        # Check if plugin manager has been started (has a host)
        if self._plugin_manager._host is None:
            return
        await self._plugin_manager.reload_installed()

    @property
    def plugin_runtime(self) -> PluginManager:
        if self.role == "control":
            if self._control_plugin_runtime is None:
                from bianbu_agent.security.runtime_process.control import ControlPluginFacade
                self._control_plugin_runtime = ControlPluginFacade()
            return self._control_plugin_runtime
        return self._plugin_manager

    @property
    def has_default_model(self) -> bool:
        """Whether an Expert without a model falls back to the injected default."""

        return self._models.has_default_model

    @property
    def runtime(self) -> RunManager:
        if not self._started:
            raise RuntimeError("Runtime is not started")
        return self.runs

    def cleanup_retired_packages(self) -> int:
        """Remove unpublished package versions once no Control run can use them."""

        if self.role != "control":
            return 0
        pool = getattr(self, "worker_supervisor", None)
        if any(
            bool(getattr(worker, "catalog_stale", False))
            for worker in getattr(pool, "_supervisors", {}).values()
        ):
            return 0
        runs = getattr(self, "runs", None)
        if runs is not None and int(getattr(runs, "active_count", 0)) > 0:
            return 0
        from bianbu_agent._internal.locking import CapabilityFilesystemLock

        removed = 0
        with CapabilityFilesystemLock(self.home.root, exclusive=True):
            for path in tuple(self.home.staging_dir.glob("retired-*")):
                if path.parent != self.home.staging_dir:
                    continue
                try:
                    if path.is_symlink() or not path.is_dir():
                        path.unlink(missing_ok=True)
                    else:
                        shutil.rmtree(path)
                    removed += 1
                except OSError as exc:
                    logger.warning(
                        "Deferred capability cleanup failed for %s: %s",
                        path.name,
                        type(exc).__name__,
                    )
        return removed

    async def __aenter__(self) -> BianbuAgentRuntime:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def start(self) -> BianbuAgentRuntime:
        if self._started:
            return self
        self.home.prepare()
        if self.role == "control":
            retired_records = self.agent_packages.retire_unloadable_records()
            if retired_records:
                logger.warning(
                    "Retired %d unloadable Agent record(s): %s",
                    len(retired_records),
                    ", ".join(retired_records),
                )
            self.cleanup_retired_packages()
        self.runtime_log_store.install()
        try:
            # 第一阶段商用入口固定让控制面运行在 host。将 control
            # requested_mode 配成 sandbox 不能被静默忽略；该路径需要
            # socket activation/host proxy，未实现前必须在启动时明确拒绝。
            if (
                self.role == "control"
                and self.config.runtime.control.execution.requested_mode != "host"
            ):
                raise RuntimeStartupError(
                    "控制面 sandbox 尚未实现：请将 "
                    "runtime.control.execution.requested_mode 设置为 host"
                )
            # securityd 以 D-Bus sender 的 UID/executable/cgroup 识别调用方。
            # 必须由 Runtime 进程自身在启动阶段绑定，测试进程或配置台
            # 代绑都会形成错误身份，严格 caller 校验下随后必然被拒绝。
            # The control plane must bind the stable Security Agent identity
            # because it is the caller of Prepare/Commit/Revoke.  A managed
            # Worker uses the lease-bound UDS path and never replaces this
            # process-level binding.
            if self.role == "control" or (
                self.role == "worker" and
                os.environ.get("BIANBU_WORKER_MANAGED") != "1"
            ):
                await _bind_security_identity()
            # The control plane never constructs a LangGraph engine or opens
            # the Worker's checkpoint database. It only owns API/session/run
            # metadata and forwards execution through Worker RPC.
            if self.role == "worker":
                # The Worker owns the PID observed by memoryd. Pass the
                # Runtime Agent id explicitly so the SDK hello context and
                # trusted registry use the same identity.
                worker_agent_id = (
                    self._worker_agent_id
                    or os.environ.get("COM_BIANBU_AGENT_ID")
                    or os.environ.get("BIANBU_WORKER_AGENT_ID")
                    or ""
                ).strip()
                if not worker_agent_id:
                    raise RuntimeStartupError("Worker requires COM_BIANBU_AGENT_ID")
                validate_agent_id(worker_agent_id)
                if not os.environ.get("BIANBU_AGENT_MEMORY_AGENT_NAME", "").strip():
                    os.environ["BIANBU_AGENT_MEMORY_AGENT_NAME"] = worker_agent_id
                # A managed Worker is attested and registered by its Supervisor
                # using the host PID. Do not overwrite that registration on activate.
                if os.environ.get("BIANBU_WORKER_MANAGED") != "1":
                    self._memory_registration_path = register_memory_runtime(
                        agent_name=worker_agent_id
                    )
                    if self._memory_registration_path is not None:
                        self._memory_registration = MemoryRegistration(self._memory_registration_path)
                await self._prepare_engine()
            for store in (
                self.model_configs,
                self._agent_store,
                self._session_store,
                self._run_store,
            ):
                opener = getattr(store, "open", None)
                if callable(opener):
                    await opener()
                    self._opened.append(store)
            if self.role == "worker":
                generation = await self._plugin_manager.start()
                if self.config.require_file_backed_capabilities:
                    _validate_file_backed(generation)
            else:
                from bianbu_agent.security.runtime_process.control import EmptyGeneration
                generation = EmptyGeneration()
            self.agents = AgentManager(
                store=self._agent_store,
                model_registry=self._models,
                generation_provider=lambda: self.plugin_runtime.current_generation,
                presets=self.config.presets,
            )
            if self.role == "worker":
                await self.agents.materialize(
                    worker_agent_id,
                    generation,
                    allow_model_repair=True,
                    allow_capability_gap=True,
                )
            if (
                self.role == "worker"
                and os.environ.get("BIANBU_WORKER_MANAGED") != "1"
                and not _is_unmanaged_worker_fallback()
            ):
                await self._prepare_security_runtime()
            if self._security_runtime_client is not None:
                self.execution_coordinator.security_client = self._security_runtime_client
            elif (
                os.environ.get("BIANBU_SECURITY", "0") == "1"
                or str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production"
                or (
                    self.config.security.required
                    and (os.environ.get("BIANBU_SECURITYD_HOME")
                         or os.environ.get("BIANBU_SECURITY_CLIENT_DIR"))
                )
            ):
                self._security_runtime_client = SecurityRuntimeClient(
                    # run.sh selects development optional/required behavior
                    # through BIANBU_RUNTIME_SECURITY.  Passing the config
                    # default explicitly would silently turn that opt-in
                    # development setting back into fail-closed mode.
                    required=(
                        None
                        if "BIANBU_RUNTIME_SECURITY" in os.environ
                        else self.config.security.required
                    )
                )
            # Register all logical Agents with securityd independently from
            # the Runtime process lease.  In development this is best-effort;
            # production deployments may require the registration client.
            for logical_agent in await self.agents.list():
                try:
                    await self.register_logical_agent(logical_agent)
                except Exception as exc:
                    self._security_pending[logical_agent.id] = str(exc)
                    if self.security_registration_required:
                        raise
                    logger.warning("logical Agent security registration unavailable: %s", exc)
            if self.role == "worker":
                await self.execution_coordinator.reap_orphans()
            self.runs = RunManager(
                plugin_manager=self._plugin_manager,
                agent_manager=self.agents,
                model_registry=self._models,
                engine_builder=self._engine_builder,
                store=self._run_store,
                max_concurrent_runs=(
                    self.config.runtime.worker.max_concurrent_runs
                ),
            )
            if self.role == "control":
                # The control process owns session/run metadata and forwards
                # execution to the separately supervised Worker. It does not
                # prepare a process-level Agent lease for itself.
                from bianbu_agent.security.runtime_process.supervisor import (
                    AgentWorkerPool,
                    ControlRunManager,
                )
                self.worker_supervisor = AgentWorkerPool(self.config, metrics=self.metrics)
                self.runs = ControlRunManager(
                    self._run_store,
                    self.worker_supervisor,
                    on_idle=self.cleanup_retired_packages,
                )
                self.worker_supervisor.run_manager = self.runs
                # Register modes without launching idle experts. Bibit warms in
                # the background; HTTP readiness never waits for a Worker.
                from bianbu_agent.provisioning import system_starter_id
                bibit_id = system_starter_id(self.config.data_dir)
                for logical_agent in await self.agents.list():
                    self.worker_supervisor.for_agent(
                        logical_agent.id,
                        requested_mode=self._requested_execution_mode(logical_agent),
                    )
                    if logical_agent.id == bibit_id:
                        self.worker_supervisor.warm_agent(logical_agent.id)
            self.sessions = SessionManager(
                store=self._session_store,
                plugin_manager=self._plugin_manager,
                run_factory=self.runs.run,
            )
            if self.role == "control":
                # SessionHandle captures this factory at construction time;
                # replace the manager after the control Run facade exists.
                self.sessions = SessionManager(
                    store=self._session_store,
                    plugin_manager=self._plugin_manager,
                    run_factory=self.runs.run,
                )
            self._started = True
            self._closing = False
            self._active_plugin_services = list(self._managed_plugin_services) if self.role == "worker" else []
            if self.role == "worker":
                await self._start_plugin_services(generation)
                self.prepare_installed_mcps()
            await self._maintain_provider_capture()
            maintenance_stop = asyncio.Event()
            self._provider_capture_maintenance_stop = maintenance_stop
            self._provider_capture_maintenance_task = asyncio.create_task(
                self._provider_capture_maintenance_loop(maintenance_stop),
                name="provider-capture-maintenance",
            )
            if self._memory_registration is not None:
                self._memory_maintenance_stop.clear()
                self._memory_maintenance_task = asyncio.create_task(
                    self._memory_registration.run(self._memory_maintenance_stop),
                    name="memory-registration-maintenance",
                )
            return self
        except Exception as exc:
            self._recent_errors.append(str(exc))
            try:
                await self._cleanup()
            except Exception as cleanup_exc:
                self._recent_errors.append(f"startup cleanup failed: {cleanup_exc}")
            raise RuntimeStartupError(f"Runtime startup failed: {exc}") from exc

    async def close(self) -> None:
        if self._closing:
            return
        if (
            not self._started
            and not self._opened
            and not self._active_plugin_services
            and self._memory_registration_path is None
            and self._provider_capture_maintenance_task is None
            and self._provider_capture_maintenance_stop is None
        ):
            return
        self._closing = True
        errors: list[Exception] = []
        cancellations: list[asyncio.CancelledError] = []
        try:
            await _collect_async_error(
                errors,
                cancellations,
                self._stop_provider_capture_maintenance,
            )
            if hasattr(self, "runs"):
                await _collect_async_error(errors, cancellations, self.runs.close)
            await _collect_async_error(
                errors, cancellations, self._close_plugin_services
            )
            await _collect_async_error(
                errors, cancellations, self._plugin_manager.close
            )
            await _collect_async_error(errors, cancellations, self._close_stores)
            await _collect_async_error(errors, cancellations, self.execution_coordinator.close)
            if hasattr(self, "worker_supervisor"):
                await _collect_async_error(errors, cancellations, self.worker_supervisor.close)
            await _collect_async_error(
                errors, cancellations, self._close_security_runtime
            )
            await _collect_async_error(
                errors, cancellations,
                self._close_memory_registration,
            )
        finally:
            self._started = False
            self._closing = False
            self.runtime_log_store.uninstall()
        if cancellations:
            raise cancellations[0]
        if errors:
            raise ExceptionGroup("Runtime close failed", errors)

    async def _cleanup(self) -> None:
        try:
            await self._stop_provider_capture_maintenance()
        finally:
            try:
                await self._close_plugin_services()
            finally:
                try:
                    await self._plugin_manager.close()
                finally:
                    await self._close_stores()
                    await self.execution_coordinator.close()
                    if hasattr(self, "worker_supervisor"):
                        await self.worker_supervisor.close()
                    await self._abort_security_runtime_start()
                    await self._close_memory_registration()
                    self._started = False
                    self.runtime_log_store.uninstall()

    async def _close_memory_registration(self) -> None:
        self._memory_maintenance_stop.set()
        task = self._memory_maintenance_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._memory_maintenance_task = None
        if self._memory_registration is not None:
            self._memory_registration.close()
            self._memory_registration = None
        elif self._memory_registration_path is not None:
            unregister_memory_runtime(self._memory_registration_path)
        self._memory_registration_path = None

    async def _maintain_provider_capture(self) -> None:
        try:
            await asyncio.to_thread(self.provider_capture_store.maintain)
        except Exception:
            logger.exception("Provider capture maintenance failed")

    async def _provider_capture_maintenance_loop(
        self,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.01, PROVIDER_CAPTURE_MAINTENANCE_SECONDS)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
            if stop.is_set():
                break
            await self._maintain_provider_capture()

    async def _stop_provider_capture_maintenance(self) -> None:
        task = self._provider_capture_maintenance_task
        stop = self._provider_capture_maintenance_stop
        if stop is not None:
            stop.set()
        if task is None:
            self._provider_capture_maintenance_stop = None
            return
        cancellation: asyncio.CancelledError | None = None
        try:
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError as exc:
                    current = asyncio.current_task()
                    if task.cancelled() and (
                        current is None or current.cancelling() == 0
                    ):
                        raise
                    cancellation = cancellation or exc
                    if task.done():
                        break
            task.result()
        finally:
            if self._provider_capture_maintenance_task is task:
                self._provider_capture_maintenance_task = None
            if self._provider_capture_maintenance_stop is stop:
                self._provider_capture_maintenance_stop = None
        if cancellation is not None:
            raise cancellation

    async def _start_plugin_services(self, generation: object) -> None:
        for service in self._active_plugin_services:
            starter = getattr(service, "start", None)
            if callable(starter):
                result = starter(self, generation)
                if inspect.isawaitable(result):
                    await result

    async def _reload_after_mcp_preparation(self) -> None:
        """Publish the tools of MCP servers whose background start finished.

        A prepared server only becomes usable once a Generation starts it, so
        the preparation has to end in a reload.  There is no lease here: the
        reload takes the Plugin manager lock like any other caller, and a Run
        that already holds a lease keeps its own Generation.
        """

        if not self._started or self.role != "worker":
            return
        await self._plugin_manager.reload_installed(force=True)

    def prepare_installed_mcps(self) -> None:
        """Start every installed MCP server in the background.

        ``mcp add`` only registers a declaration, so the Worker is the one that
        has to turn an ``npx`` declaration into a local installation.  It does
        that for everything in the catalog, whether or not a selected Agent
        declares it: the caller that installed the server is long gone, and an
        installation that is only ever attempted from a Generation build would
        leave the server unusable until something happened to select it.

        This is also where a server's tools are learned, which is what every
        surface shows for a Server no Expert selected yet, so an install runs
        it again instead of waiting for the next Worker restart.
        """

        preparation = self.mcp_preparation
        if preparation is None:
            return
        try:
            installed = self.mcps.list()
        except Exception as exc:  # noqa: BLE001 - a broken catalog must not fail startup
            logger.warning("installed MCP catalog unavailable: %s", exc)
            return
        for record in installed:
            source = record.root / "mcp.json"
            try:
                config = load_mcp_config(source, workspace=self.home.workspace_dir)
            except Exception as exc:  # noqa: BLE001 - one bad server must not hide the rest
                logger.warning("MCP server %s is not readable: %s", record.id, exc)
                continue
            preparation.ensure(record.id, config, source=source)

    async def _close_plugin_services(self) -> None:
        errors: list[Exception] = []
        cancellations: list[asyncio.CancelledError] = []
        remaining: list[object] = []
        for service in reversed(self._active_plugin_services):
            closer = getattr(service, "close", None)
            try:
                if not callable(closer):
                    continue
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError as exc:
                remaining.append(service)
                cancellations.append(exc)
            except Exception as exc:
                remaining.append(service)
                errors.append(exc)
        self._active_plugin_services = list(reversed(remaining))
        if cancellations:
            raise cancellations[0]
        if errors:
            raise ExceptionGroup("managed Plugin service close failed", errors)

    async def _close_stores(self) -> None:
        for store in reversed(self._opened):
            closer = getattr(store, "close", None)
            if callable(closer):
                await closer()
        self._opened.clear()

    async def _prepare_engine(self) -> None:
        if self._configured_engine_builder is not None:
            self._engine_builder = self._configured_engine_builder
            return

        async def transcript(session_id: str):
            reader = getattr(self._run_store, "read_session_events", None)
            if callable(reader):
                return await reader(session_id)
            # A sandbox Worker owns no durable transcript of its own: the
            # control plane writes one in the shared data directory and feeds
            # it the mirrored events, so the Worker folds that log read-only
            # instead of its own memory store.
            return await asyncio.to_thread(
                read_session_transcript, self.home.sessions_dir, session_id
            )

        def default_engine(agent, generation, model):
            return build_default_engine(
                agent,
                generation,
                model,
                transcript_provider=transcript,
                capture_callback=self.provider_capture_callback,
                context_settings=CompressionSettingsProvider(),
            )

        self._engine_builder = default_engine

    def _requested_execution_mode(self, agent) -> str:
        """Report the mode one Agent runs in.

        Whether an Agent needs a sandbox follows from the security settings it
        declares in the console, and securityd is the authority that decides
        it. The Runtime therefore only reports a hint: ``auto`` asks
        security.runtime to apply its own decision instead of naming a side.
        """
        return "auto"

    async def _prepare_security_runtime(self) -> None:
        """Preflight and commit the Runtime process against security.runtime.

        This does not claim to create a child sandbox.  It proves that the
        process which serves Runtime is running under the effective mode selected by
        securityd.  Full per-Agent child backends can reuse this coordinator
        once they own process creation.
        """
        import os
        production = str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production"
        if os.environ.get("BIANBU_SECURITY", "1") == "0":
            if production:
                raise RuntimeStartupError("生产模式禁止通过 BIANBU_SECURITY=0 绕过 Security Runtime")
            return
        if not os.environ.get("BIANBU_SECURITYD_HOME", "").strip() and not production:
            return
        self._security_runtime_client = SecurityRuntimeClient()
        # Securityd's stable agent key is intentionally a separate namespace
        # from Runtime's local ``AgentSpec.id``. The signed manifest owns the
        # fixed entrypoint; Runtime only submits the namespace and mode request.
        agent_id = os.environ.get("BIANBU_AGENT_ID", "com.bianbu.agent").strip()
        logical_agent_id = (
            os.environ.get("COM_BIANBU_AGENT_ID")
            or os.environ.get("BIANBU_WORKER_AGENT_ID")
            or ""
        ).strip()
        agent = await self.agents.get(logical_agent_id)
        request = RuntimeStartRequest(
            agent_id=agent_id,
            requested_mode=self._requested_execution_mode(agent),
            worker_agent_id=logical_agent_id,
        )
        decision = await self._security_runtime_client.prepare_start(request)
        if decision is None:
            # Development optional mode intentionally decouples logical Agent
            # registration from the legacy process lease.  A missing or
            # incompatible securityd must not prevent the local Worker from
            # serving conversations; production/required mode remains strict.
            if not self._security_runtime_client.required:
                logger.warning("security.runtime 未返回 PA 启动决策，开发 optional 模式继续")
                return
            raise RuntimeStartupError("security.runtime 未返回 PA 启动决策")
        # The current PA process must already be in the mode securityd chose;
        # silently continuing outside it would turn PrepareStart into a no-op.
        active_sandbox = str(os.environ.get("BIANBU_SANDBOX_ACTIVE", "0")).lower() in {
            "1", "true", "yes"
        }
        # A caller that explicitly selected sandbox is expected to set the
        # development marker.  For ``auto``/``host`` the host backend is the
        # current Runtime process itself; a missing marker is therefore valid.
        if decision.effective_mode == "sandbox" and not active_sandbox:
            await self._security_runtime_client.abort_start(
                decision.lease_id, "effective_mode=sandbox 但 Runtime 未处于 sandbox")
            raise RuntimeStartupError("security.runtime 要求 sandbox，但当前 Runtime 未在 sandbox 中运行")
        if decision.effective_mode == "host" and active_sandbox:
            await self._security_runtime_client.abort_start(
                decision.lease_id, "effective_mode=host 但 Runtime 仍在 sandbox")
            raise RuntimeStartupError("security.runtime 允许 host，但当前 Runtime 仍在 sandbox 中")
        # bwrap PID namespaces restart the Runtime with the same namespace-local
        # PID (commonly 6). Include the host process start-time token so a
        # restart cannot collide with a previous committed instance row.
        try:
            start_token = Path(f"/proc/{os.getpid()}/stat").read_text().split()[21]
        except (OSError, IndexError, ValueError):
            start_token = str(time.time_ns())
        instance_id = f"agent-runtime-{os.getpid()}-{start_token}"
        cgroup_path, attestation = current_attestation(
            decision, pid=os.getpid(), instance_id=instance_id)
        # Inside bwrap, Runtime sees a remapped PID namespace and a synthetic
        # cgroup root (``/``), while securityd receives the host PID/cgroup
        # from the authenticated D-Bus sender.  Passing the namespace-local
        # values would make CommitStart compare unlike coordinate systems.
        # Zero/empty values explicitly ask the service to resolve the host
        # coordinates from SO_PEERCRED and /proc, while retaining the
        # namespace attestation (sandboxed/container_id) for verification.
        commit_pid = os.getpid()
        commit_cgroup = cgroup_path
        commit_attestation = attestation
        if active_sandbox:
            commit_pid = 0
            commit_cgroup = ""
            commit_attestation = {**attestation, "pid": 0}
        try:
            await self._security_runtime_client.commit_start(
                decision.lease_id, instance_id, commit_pid, commit_cgroup,
                commit_attestation)
        except Exception:
            # CommitStart consumes the one-shot lease before recording its
            # audit event. AbortStart is valid only before Commit; after a
            # committed-but-failed response securityd has already marked the
            # lease terminal and the backend cleanup below is sufficient.
            try:
                await self._security_runtime_client.abort_start(
                    decision.lease_id, "Runtime CommitStart 失败")
            except Exception as cleanup_exc:  # noqa: BLE001
                logger.warning("CommitStart 失败后的 AbortStart 已不可用：%s", cleanup_exc)
            raise
        self._security_runtime_decision = decision
        self._security_runtime_instance_id = instance_id
        logger.info(
            "security.runtime lease 已提交 agent=%s mode=%s backend=%s lease=%s",
            agent_id, decision.effective_mode, decision.backend, decision.lease_id,
        )

    async def _abort_security_runtime_start(self) -> None:
        decision = self._security_runtime_decision
        client = self._security_runtime_client
        if decision is not None and client is not None:
            try:
                await client.revoke_agent(
                    decision.policy.get("agent_id", "com.bianbu.runtime"),
                    "Runtime startup cleanup",
                )
            except Exception:  # noqa: BLE001
                logger.exception("security.runtime cleanup failed")
        self._security_runtime_decision = None

    async def _close_security_runtime(self) -> None:
        await self._abort_security_runtime_start()

    @property
    def executions(self) -> ExecutionCoordinator:
        """受安全策略约束的 Agent 实例协调器。"""
        return self.execution_coordinator

    async def apply_network_learning_policy(
        self, agent_id: str, mode: str, version: int,
    ) -> dict[str, int]:
        pool = getattr(self, "worker_supervisor", None)
        if self.role == "control" and pool is not None:
            return await pool.apply_network_learning_policy(agent_id, mode, version)
        return await self.execution_coordinator.apply_network_learning_policy(
            agent_id, mode, version,
        )

    async def start_execution(self, request: ExecutionRequest) -> ExecutionRecord:
        """启动一个由 security.runtime 授权的 host/sandbox Agent 实例。"""
        if not self._started:
            raise RuntimeError("Runtime is not started")
        return await self.execution_coordinator.start(request)

    async def start_agent_execution(
        self,
        agent_id: str,
        *,
        requested_mode: str = "host",
        env: Mapping[str, str] | None = None,
        target_port: int | None = None,
        listen_port: int | None = None,
    ) -> ExecutionRecord:
        """按已安装 Agent manifest 启动受控 child Agent。

        调用者只能选择模式偏好和受限运行参数；entrypoint 和工作目录由
        Runtime 从已安装包和 securityd 策略计算，绝不
        从 HTTP 请求接受 command/cwd。
        """
        if not self._started:
            raise RuntimeError("Runtime is not started")
        local_id = str(agent_id).strip()
        package = self.agent_packages.inspect(local_id)
        if package is None:
            raise KeyError(local_id)
        metadata = dict(package.manifest.spec.metadata or {})
        security_id = str(metadata.get("security_agent_id") or local_id).strip()
        if not security_id:
            raise RuntimeStartupError(
                f"Agent {local_id} 未声明 metadata.security_agent_id，拒绝启动")
        declared_cwd = metadata.get("working_directory")
        return await self.execution_coordinator.start_registered(
            agent_id=security_id,
            requested_mode=requested_mode,
            env=env,
            cwd=str(declared_cwd) if declared_cwd else None,
            target_port=target_port,
            listen_port=listen_port,
        )

    async def revoke_agent_execution(self, agent_id: str, *, reason: str = "requested") -> int:
        return await self.execution_coordinator.revoke(str(agent_id).strip(), reason=reason)

    async def stop_execution(self, instance_id: str, *, reason: str = "requested") -> bool:
        """停止实例并撤销其本地端口代理。"""
        return await self.execution_coordinator.stop(instance_id, reason=reason)

    async def diagnostics(self) -> RuntimeDiagnostics:
        generation = (
            self.plugin_runtime.current_generation if self._started else None
        )
        return RuntimeDiagnostics(
            started=self._started,
            closing=self._closing,
            generation_id=generation.id if generation is not None else None,
            inventory_revision=(
                generation.capability_catalog.inventory_revision
                if generation is not None and generation.capability_catalog is not None
                else None
            ),
            active_runs=self.runs.active_count if hasattr(self, "runs") else 0,
            waiting_runs=self.runs.waiting_count if hasattr(self, "runs") else 0,
            plugin_statuses=(
                tuple(
                    (
                        plugin_id,
                        status.state.value,
                        status.health.status,
                        (
                            _contributed_capability_ids(
                                generation.plugins[plugin_id]
                            )
                            if plugin_id in generation.plugins
                            else ()
                        ),
                    )
                    for plugin_id, status in sorted(generation.statuses.items())
                )
                if generation is not None
                else ()
            ),
            recent_errors=tuple(self._recent_errors[-10:]),
            security_pending=tuple(sorted(self._security_pending.items())),
        )

    @property
    def security_pending(self) -> dict[str, str]:
        """Logical Agent IDs whose registration/sync is awaiting securityd."""
        return dict(self._security_pending)

    @property
    def security_registration_required(self) -> bool:
        if self.role == "worker" and os.environ.get("BIANBU_WORKER_MANAGED") == "1":
            return False
        if str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production":
            return True
        if self._security_runtime_client is None:
            return False
        return bool(self._security_runtime_client.required)

    def _security_registration_enabled(self) -> bool:
        if self.role == "worker" and os.environ.get("BIANBU_WORKER_MANAGED") == "1":
            return False
        if self._security_runtime_client is None:
            return False
        # A client object may exist because config.security.required defaults to
        # true; do not probe D-Bus unless a client boundary was explicitly
        # provisioned (or an installed native client can be imported).
        if str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production":
            return True
        return bool(os.environ.get("BIANBU_SECURITYD_HOME") or os.environ.get("BIANBU_SECURITY_CLIENT_DIR")) or os.environ.get("BIANBU_SECURITY") == "1"

    async def register_logical_agent(self, spec) -> object | None:
        """Register and replace-sync one logical Agent declaration.

        This is the single lifecycle entry point used by startup and API CRUD.
        ``None`` means no security client was configured; failures are raised
        for the caller to classify as pending (development) or fatal.
        """
        if not self._security_registration_enabled():
            if self.security_registration_required:
                raise SecurityRuntimeUnavailable("生产模式未配置 Security Agent 注册客户端")
            return None
        registration = SecurityAgentRegistration(self._security_runtime_client)
        result = await registration.register(spec.id, {
            "name": spec.name,
            "version": str(getattr(spec, "version", "1.0.0")),
            # securityd keeps this so the console can list only the tools that
            # belong to the Agent's framework (runtime vs hermes).
            "framework": agent_framework(spec),
        })
        # Only add tool rows securityd has not seen yet. Tool policies, DLP
        # rows, filesystem scopes and the execution boundary are all owned by
        # the security console; a re-registration must never roll back an
        # operator's tightening.
        synced = await registration.ensure_capabilities(
            spec.id, logical_agent_capabilities(spec))
        if result is None or synced is None:
            # SecurityRuntimeClient deliberately returns None in optional mode
            # when the helper/D-Bus endpoint is unavailable. Preserve this as
            # an explicit pending state instead of reporting a false success.
            self._security_pending[spec.id] = "securityd unavailable"
            return None
        from bianbu_agent.provisioning import system_starter_id
        if spec.id == system_starter_id(self.config.data_dir):
            initialized = await registration.initialize_builtin_yolo(spec.id)
            if initialized is None:
                self._security_pending[spec.id] = "securityd mode unavailable"
                return None
        self._security_pending.pop(spec.id, None)
        return result

    async def unregister_logical_agent(self, agent_id: str) -> object | None:
        if not self._security_registration_enabled():
            return None
        result = await SecurityAgentRegistration(self._security_runtime_client).unregister(agent_id)
        self._security_pending.pop(str(agent_id), None)
        return result

    def runtime_status(self) -> dict[str, object]:
        """Return the public, non-secret readiness contract.

        ``CONTROL_READY`` means the HTTP/control metadata plane is usable;
        ``AGENT_READY`` is reported only after a Worker completed Hello,
        activation and lease commit.  A failed or missing Worker is
        ``DEGRADED`` and never silently executes in the control process.
        """
        control_ready = bool(self._started and not self._closing)
        worker = getattr(self, "worker_supervisor", None)
        worker_view = worker.status_snapshot() if worker is not None else {
            "state": "stopped", "instance_id": None, "pid": None,
            "effective_mode": None, "backend": None, "sandboxed": False,
            "generation_id": None,
        }
        agent_ready = bool(worker is not None and worker.ready)
        if not control_ready:
            status = "STOPPING" if self._closing else "STARTING"
        elif agent_ready:
            status = "AGENT_READY"
        elif worker is not None and worker_view.get("state") == "failed":
            status = "DEGRADED"
        else:
            status = "CONTROL_READY"
        return {
            "status": status,
            "role": self.role,
            "control_ready": control_ready,
            "agent_ready": agent_ready,
            "service_user": str(
                os.environ.get("BIANBU_RUNTIME_SERVICE_USER", "")
            ).strip(),
            "data_dir": str(self.config.data_dir),
            "instance_id": worker_view.get("instance_id"),
            "pid": worker_view.get("pid"),
            "effective_mode": worker_view.get("effective_mode"),
            "backend": worker_view.get("backend"),
            "sandboxed": bool(worker_view.get("sandboxed", False)),
            "generation_id": worker_view.get("generation_id"),
            "worker_state": str(worker_view.get("state") or "stopped"),
            "security_pending": dict(self._security_pending),
            "seccomp": dict(worker_view.get("seccomp") or {}),
            "audit": dict(worker_view.get("audit") or {}),
        }

    def runtime_workers_status(self) -> dict[str, object]:
        """Return Control and all managed Worker process statuses."""
        pool = getattr(self, "worker_supervisor", None)
        snapshot = pool.status_snapshot() if pool is not None else {}
        workers = []
        for agent_id, item in sorted((snapshot.get("workers") or {}).items()):
            row = {"agent_id": str(agent_id), **dict(item)}
            # Only copy the public, non-secret Worker fields into the contract.
            workers.append({key: (row.get(key) if row.get(key) is not None else {})
                            if key in {"seccomp", "audit"} else bool(row.get(key))
                            if key == "catalog_stale" else row.get(key)
                            for key in (
                "agent_id", "state", "instance_id", "pid", "effective_mode",
                "backend", "sandboxed", "generation_id", "error",
                "catalog_stale", "seccomp", "audit", "memory_registration",
            )})
        control_ready = bool(self._started and not self._closing)
        runtime_view = self.runtime_status()
        control_sandboxed = os.environ.get("BIANBU_SANDBOX_ACTIVE", "0").lower() in {"1", "true", "yes"}
        return {
            "runtime": runtime_view,
            "control": {
                "pid": os.getpid(),
                "state": "running" if control_ready else ("stopping" if self._closing else "starting"),
                "control_ready": control_ready,
                "role": "control",
                "effective_mode": "sandbox" if control_sandboxed else "host",
                "backend": "control-process",
                "sandboxed": control_sandboxed,
            },
            "worker_count": len(workers),
            "workers": workers,
        }


def _mcp_security_capability(mcp_id: str) -> str:
    # The Intent MCP was split into readonly/control/admin servers. Keep their
    # securityd capability mapped to the existing `mcp.bibit-intent` grant so
    # this packaging split does not silently revoke existing user grants.
    if mcp_id in {
        "bianbu.bibit.intent/bibit-intent",
        "bianbu.bibit.intent/bibit-readonly",
        "bianbu.bibit.intent/bibit-control",
        "bianbu.bibit.intent/bibit-admin",
    }:
        return "mcp.bibit-intent"
    return f"mcp.{mcp_id}"


async def _securityd_allows_mcp(agent_id: str, mcp_id: str) -> bool:
    """MCP 层 1 判权（INT-RT-08 / D-2）：整台 server 是否授权给该 Agent。

    约定 tool="mcp.<mcp_id>"。**fail-closed**：未授权就不放行。

    为什么是 fail-closed —— MCP server 是外部第三方代码，以服务用户身份运行。
    放行未授权的 server 等于"装一台 MCP 就多一批免判权工具"。

    但纯 fail-closed 有个运维陷阱：装台新 server 连不上，而运维不知道要去配
    什么。所以未注册时**自动登记为 ask 策略**，配置台就能看到"有台 server 在等
    授权"，不用去猜 server ID。

    三种情形：
      工具未注册    → 自动登记 ask + 拦截 + 日志给出授权指引
      deny          → 拦截（用户明确拒绝过，不重新登记）
      allow/ask     → 放行。ask 留给层 2 的工具粒度审批，此处不阻塞。

    securityd 不可达时放行 —— 层 2 的 before_tool hook 仍会按 fail 分档拦高危，
    不让 securityd 单点故障使全部 MCP 不可用。
    """
    import asyncio
    import logging

    logger = logging.getLogger("bianbu_agent.security")
    try:
        sc = security_client_module()
    except Exception:
        return True

    capability = _mcp_security_capability(mcp_id)

    def _check() -> bool:
        try:
            mode = sc.get_agent_security_mode(agent_id)
            if isinstance(mode, dict) and str(mode.get("mode", "")).lower() == "yolo":
                try:
                    sc.audit_log(agent_id, "mcp_access", capability, "allow;mode=yolo")
                except Exception:
                    pass
                return True
        except Exception:
            pass
        # 0=ALLOW 1=ASK 2=DENY
        decision = int(sc.check_permission(agent_id, capability))
        if decision != 2:
            return True

        # DENY —— 区分「未注册」与「已撤销」，两者处置不同
        try:
            rows = sc.get_capabilities(agent_id) or []
        except Exception as e:  # noqa: BLE001 — 查不到就按未注册处理
            logger.debug("查询能力清单失败（%s）：%s", capability, e)
            rows = []
        existing = next(
            (r for r in rows if str(r.get("tool") or r.get("capability") or "") == capability), None)

        if existing is not None:
            logger.warning(
                "MCP server %s 未获授权（%s，policy=%s），已拦截。"
                "如需启用请在 securityd 配置台把该工具策略改为 allow。",
                mcp_id, capability, existing.get("policy"))
            return False

        # 未注册 → 登记为 ask 策略，让配置台可见
        try:
            sc.register_capabilities(agent_id, [{
                "tool": capability,
                # 外部代码不给永久放行：默认 ask，由管理员显式改为 allow。
                "policy": "ask",
            }])
            logger.warning(
                "MCP server %s 首次出现，已登记 %s 为 ask 策略并拦截本次连接。"
                "请在 securityd 配置台（:8787）改为 allow 后重试。",
                mcp_id, capability)
        except Exception as e:  # noqa: BLE001 — 登记失败不改变拦截结论
            logger.warning(
                "MCP server %s 未授权且自动登记失败（%s）：%s。"
                "请手工在 securityd 配置 %s。",
                mcp_id, capability, e, capability)
        return False

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _check)
    except Exception as e:  # noqa: BLE001 — 不可达则放行，交层 2 兜
        logger.debug("MCP 层 1 判权跳过（%s）：%s", capability, e)
        return True


async def _bind_security_identity() -> bool:
    """可选地把当前 Runtime 进程绑定到 securityd Agent 身份。

    正式 bianbu-agent-runtimed 可改用 V2 接口携带 session/run；Runtime 服务启动
    尚无 run 上下文，因此这里只建立 V1 稳定进程身份。securityd 缺失时
    保持既有降级策略，不阻止 Runtime 启动。
    """
    import sys

    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        return False
    production = str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production"
    home = (os.environ.get("BIANBU_SECURITYD_HOME") or "").strip()
    agent_id = (
        os.environ.get("BIANBU_RUNTIME_SECURITY_ID") or "com.bianbu.runtime"
    ).strip()
    if not agent_id:
        return False

    def _bind() -> bool:
        try:
            sc = security_client_module()
            return bool(sc.bind_agent(agent_id))
        except Exception:
            # Python 3.14 Runtime with Debian's CPython 3.12 dbus-python uses
            # the same local helper as SecurityRuntimeClient.
            import json, subprocess
            from pathlib import Path
            helper = Path(__file__).resolve().parent.parent / "security" / "dbus_client_helper.py"
            env = os.environ.copy()
            home = env.get("BIANBU_SECURITYD_HOME", "")
            env["PYTHONPATH"] = env.get("BIANBU_SECURITY_CLIENT_DIR", "") or (str(Path(home) / "client") if home else "")
            payload = json.dumps({"method": "bind_agent", "args": [agent_id], "kwargs": {}})
            proc = subprocess.run(["/usr/bin/python3", str(helper), payload], env=env,
                                  capture_output=True, text=True, timeout=30, check=False)
            if proc.returncode:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
            return json.loads(proc.stdout)

    try:
        bound = await asyncio.to_thread(_bind)
    except Exception as exc:  # noqa: BLE001 — production must fail closed
        if production:
            raise RuntimeStartupError(
                f"生产 Runtime 无法加载/绑定 bianbu_security_client：{exc}") from exc
        logger.warning(
            "securityd Agent 身份绑定失败，将按 fail 分档降级（agent_id=%s）：%s: %s",
            agent_id, type(exc).__name__, exc,
        )
        return False
    if bound:
        logger.info("securityd Agent 身份已绑定 agent_id=%s", agent_id)
    else:
        logger.warning("securityd Agent 身份绑定返回 False agent_id=%s", agent_id)
    return bound


_INVENTORY_TABLES = ("inventory_metadata", "installed_capabilities")


def _inventory_snapshot(
    config: RuntimeConfig, *, skills_only: bool = False, strict_skills: bool = False
) -> InventorySnapshot:
    path = config.data_dir / "inventory.sqlite3"
    stored_items: list[InstalledCapability] = []
    revision = 0
    if not path.is_file() or path.stat().st_size == 0:
        rows = ()
    else:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not set(_INVENTORY_TABLES) <= tables:
                # A file without the managed schema is a placeholder, not a
                # capability Inventory: failing here would turn one stale empty
                # file into an error for every capability request.
                logger.warning(
                    "Ignoring capability Inventory without its schema: %s", path
                )
                rows = ()
                connection.rollback()
            else:
                revision_row = connection.execute(
                    "SELECT value FROM inventory_metadata WHERE key = 'revision'"
                ).fetchone()
                rows = connection.execute(
                    "SELECT * FROM installed_capabilities ORDER BY kind, capability_id"
                ).fetchall()
                revision = int(revision_row["value"] if revision_row else 0)
                connection.commit()
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise ValueError(f"invalid capability Inventory: {path}") from exc
        finally:
            connection.close()
    stored_items.extend(
            InstalledCapability(
                source=CapabilitySource(
                    kind=CapabilityKind(row["kind"]),
                    capability_id=row["capability_id"],
                    version=row["version"],
                    root=row["root"],
                    manifest_path=row["manifest_path"],
                    content_digest=row["content_digest"],
                    origin=CapabilityOrigin(row["origin"]),
                    plugin_id=row["plugin_id"],
                    metadata=json.loads(row["metadata_json"]),
                ),
                enabled=bool(row["enabled"]),
            )
            for row in rows
    )
    items = {
        (item.source.kind, item.source.capability_id): item
        for item in stored_items
        if item.source.kind not in {CapabilityKind.PLUGIN, CapabilityKind.SKILL}
        and not skills_only
        and item.source.origin is not CapabilityOrigin.PLUGIN
        and not item.source.metadata.get("system_managed")
    }
    from bianbu_agent.mcp.system import system_mcp_root
    from bianbu_agent.system_resources import system_resource_dir, is_system_resource
    from bianbu_agent._internal.filesystem import UnsafeFilesystemEntry

    def package_roots(configured: tuple[Path, ...], managed: Path) -> tuple[Path, ...]:
        # Explicit discovery roots extend, rather than hide, the package roots
        # written by the management API.
        return tuple(dict.fromkeys((*configured, managed)))

    roots = {
        CapabilityKind.PLUGIN: package_roots(
            (system_resource_dir("plugins"), *config.plugin_dirs), config.data_dir / "plugins"
        ),
        CapabilityKind.MCP: package_roots(
            (system_mcp_root(), *config.mcp_dirs), config.data_dir / "mcps"),
        CapabilityKind.SKILL: package_roots(
            (*config.skill_dirs, Path(os.environ.get("BIANBU_USER_SKILLS_DIR")
                                     or Path.home() / ".bianbu-agent" / "skills")),
            config.data_dir / "skills"
        ) + (system_resource_dir("skills"),),
    }
    for kind, directories in roots.items():
        if skills_only and kind is not CapabilityKind.SKILL:
            continue
        definition_name = {
            CapabilityKind.PLUGIN: None,
            CapabilityKind.MCP: "mcp.json",
            CapabilityKind.TOOL: "tool.py",
            CapabilityKind.SKILL: "SKILL.md",
        }[kind]
        for directory in directories:
            root = Path(directory)
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or (
                    definition_name is not None
                    and not (child / definition_name).is_file()
                ):
                    continue
                managed = (
                    kind in {CapabilityKind.PLUGIN, CapabilityKind.MCP}
                    or (child / ".bianbu-agent-sdk-ownership.json").is_file()
                )
                if (
                    kind is not CapabilityKind.SKILL
                    and not managed
                    and not config.allow_unmanaged_capabilities
                ):
                    continue
                try:
                    discovered = _file_capability(kind, child)
                except UnsafeFilesystemEntry:
                    # A redirectable package entry is an attack, not a bad file.
                    raise
                except ValueError as exc:
                    if kind not in {
                        CapabilityKind.SKILL,
                        CapabilityKind.PLUGIN,
                        CapabilityKind.MCP,
                    } or (strict_skills and kind is CapabilityKind.SKILL):
                        raise
                    # One unreadable package must not hide the rest of the
                    # catalog from the Runtime and its Control plane.
                    logger.warning(
                        "Skipping invalid %s at %s: %s", kind.value.title(), child, exc
                    )
                    continue
                if kind in {CapabilityKind.MCP, CapabilityKind.PLUGIN, CapabilityKind.SKILL} and (
                    is_system_resource(child, kind.value + "s")
                    or (kind is CapabilityKind.MCP and root == system_mcp_root())
                ):
                    discovered = replace(discovered, source=replace(
                        discovered.source, metadata={**discovered.source.metadata,
                                                     "system_managed": True}))
                key = (kind, discovered.source.capability_id)
                if kind is CapabilityKind.SKILL:
                    previous = next((item for item in stored_items
                                     if (item.source.kind, item.source.capability_id) == key), None)
                    if previous is not None:
                        discovered = replace(discovered, enabled=previous.enabled)
                if kind in {CapabilityKind.PLUGIN, CapabilityKind.MCP}:
                    items[key] = discovered
                else:
                    items.setdefault(key, discovered)
                if kind is CapabilityKind.PLUGIN:
                    # A higher-priority package replaces the complete Plugin,
                    # including members removed from its current declaration.
                    shadowed = [
                        item_key for item_key, item in items.items()
                        if item.source.origin is CapabilityOrigin.PLUGIN
                        and item.source.plugin_id == discovered.source.capability_id
                    ]
                    for item_key in shadowed:
                        del items[item_key]
                    plugin_manifest = load_installed_plugin_manifest(child)
                    mcps_root = child / "mcps"
                    if plugin_manifest.contributions.mcps:
                        if mcps_root.is_symlink() or not mcps_root.is_dir():
                            raise ValueError("Plugin mcps must be a regular directory")
                        for mcp_name in plugin_manifest.contributions.mcps:
                            mcp_root = mcps_root / mcp_name
                            if mcp_root.is_symlink() or not mcp_root.is_dir():
                                raise ValueError(
                                    "Plugin mcps members must be regular directories"
                                )
                            nested = _file_capability(CapabilityKind.MCP, mcp_root)
                            public_id = f"{discovered.source.capability_id}/{mcp_root.name}"
                            nested = replace(
                                nested,
                                source=replace(
                                    nested.source,
                                    capability_id=public_id,
                                    origin=CapabilityOrigin.PLUGIN,
                                    plugin_id=discovered.source.capability_id,
                                ),
                            )
                            items[(CapabilityKind.MCP, public_id)] = nested
    return InventorySnapshot(revision=revision, items=tuple(items.values()))


def _agent_selects_mcp(agent: AgentSpec, mcp_id: str) -> bool:
    """Whether a stored Agent's declaration reaches this MCP server.

    A server contributed by a Plugin is selected through ``plugins``; a
    standalone one is selected through ``mcps``, where the expert may name it
    by its APM name (``nws``) or by the installed package ID (``mcp.nws``).
    """

    owner = mcp_id.rpartition("/")[0]
    if owner and agent.plugins is not None:
        return "*" in agent.plugins or owner in agent.plugins
    return selects_mcp(agent.mcps, mcp_id)


def _file_capability(kind: CapabilityKind, root: Path) -> InstalledCapability:
    root = root.resolve()
    files = tuple(
        path
        for path in regular_files(root)
        if path.relative_to(root).parts[:1] != ("data",)
    )
    digest = digest_directory(root, files)
    if kind is CapabilityKind.TOOL:
        inspection = DirectoryToolLoader.inspect(root)
        capability_id, version = inspection.name, inspection.version
        digest = inspection.content_digest
    elif kind is CapabilityKind.SKILL:
        contribution = DirectorySkillLoader().load(root)
        capability_id = contribution.spec.name
        version = contribution.spec.version
    elif kind is CapabilityKind.MCP:
        validate_mcp_config(root / "mcp.json", workspace=root.parent.parent / "workspace")
        capability_id = root.name
        version = "0.0.0"
        definition_path = root / "mcp.json"
    else:
        package = load_installed_plugin_manifest(root)
        capability_id, version = package.id, package.version
        definition_path = plugin_definition_path(root)
    if kind is CapabilityKind.TOOL:
        definition_path = root / "tool.py"
    elif kind is CapabilityKind.SKILL:
        definition_path = root / "SKILL.md"
    return InstalledCapability(
        source=CapabilitySource(
            kind=kind,
            capability_id=capability_id,
            version=version,
            root=root,
            manifest_path=definition_path,
            content_digest=digest,
            origin=CapabilityOrigin.STANDALONE,
            metadata={"discovered": True},
        ),
        enabled=True,
    )


def _validate_file_backed(generation) -> None:
    for kind, contributions, manifest_name in (
        ("Tool", generation.tools.values(), "tool.py"),
        ("Skill", generation.skills.values(), "SKILL.md"),
    ):
        for contribution in contributions:
            metadata = contribution.spec.metadata
            if metadata.get("origin") == "mcp" and _is_file_backed_mcp_tool(
                generation, contribution
            ):
                continue
            manifest = Path(
                str(
                    metadata.get("source_path")
                    if kind == "Tool"
                    else metadata.get("manifest_path") or ""
                )
            )
            source = Path(str(metadata.get("source_path") or ""))
            expected_name = manifest_name
            if kind == "Tool" and metadata.get("builtin"):
                from bianbu_agent.tools.builtins import BUILTIN_MODULES, builtin_source
                name = contribution.spec.name
                if name in BUILTIN_MODULES and source.resolve() == builtin_source(name).resolve():
                    expected_name = f"{name}.py"
            if manifest.name != expected_name or not manifest.is_file() or not source.is_file():
                raise ValueError(
                    f"{kind} {contribution.spec.name} is not backed by visible files"
                )


def _is_file_backed_mcp_tool(generation: object, contribution: object) -> bool:
    catalog = getattr(generation, "capability_catalog", None)
    plugin_id = str(getattr(contribution, "plugin_id", "") or "")
    mcp_sources = getattr(catalog, "mcp_sources", {})
    source = mcp_sources.get(plugin_id) if isinstance(mcp_sources, Mapping) else None
    if source is None:
        return False
    expected = (source.root / "mcp.json").resolve()
    metadata = contribution.spec.metadata
    declared = Path(str(metadata.get("source_path") or "")).resolve()
    if (
        metadata.get("runtime_id") != plugin_id
        or metadata.get("plugin_id") != plugin_id
        or expected.parent != source.root
        or declared != expected
        or expected.is_symlink()
        or not expected.is_file()
    ):
        return False
    files = tuple(
        path
        for path in regular_files(source.root)
        if path.relative_to(source.root).parts[:1] != ("data",)
    )
    return digest_directory(source.root, files) == source.content_digest


__all__ = ["BianbuAgentRuntime", "RuntimeDiagnostics"]
