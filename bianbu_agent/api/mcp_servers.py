"""Typed administration resources for the active MCP server catalog."""

from __future__ import annotations

from collections.abc import Mapping
import asyncio
import logging
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from bianbu_agent.api.contracts import (
    MCPServerCollectionEnvelope,
    MCPServerInstallRequest,
    MCPServerToolView,
    MCPServerView,
    SuccessEnvelope,
)
from bianbu_agent.api.errors import success
from bianbu_agent.api.mcp_health import MCPHealthCache, MCPHealthSnapshot
from bianbu_agent.api.capabilities import _read_mcp_descriptor, control_catalog_snapshot
from bianbu_agent.api.packages import (
    _cleanup_retired_if_idle,
    _reload_active_worker,
    _reload_active_worker_bounded,
)
from bianbu_agent.mcp.lifecycle import (
    MCP_STATE_DEGRADED,
    MCP_STATE_FAILED,
    MCP_STATE_NOT_LOADED,
    MCP_STATE_PREPARING,
    MCP_STATE_READY,
    MCP_STATE_STOPPED,
    MCP_STATE_UNKNOWN,
    mcp_lifecycle_state,
    mcp_phase,
    mcp_state_ready,
)
from bianbu_agent.mcp.managed import parse_managed_mcp, write_managed_mcp_package
from bianbu_agent.mcp.vendor import remove_vendored_server
from bianbu_agent.plugins import PluginGeneration


router = APIRouter(prefix="/mcp-servers")
health_router = APIRouter(prefix="/mcp-health-checks")
logger = logging.getLogger(__name__)


def _status_detail(status: object) -> str:
    """The first diagnostic of a failed Plugin, as one operator-facing line."""

    diagnostics = getattr(status, "diagnostics", ()) or ()
    for diagnostic in diagnostics:
        message = str(getattr(diagnostic, "message", "") or "").strip()
        if message:
            return message
    return ""


def _tool_views(raw_tools: object) -> list[MCPServerToolView]:
    """Project MCP tool payloads onto the public resource contract.

    A Worker keeps the server-declared MCP annotations (`read_only`,
    `destructive`, `requires_approval`, `mcp_annotations`) in the ToolSpec
    metadata where the approval policy reads them.  They are not part of this
    resource contract, and extra fields must not turn a healthy server into a
    validation error, so only the contract fields survive the projection.
    """

    if not isinstance(raw_tools, (list, tuple)):
        raise ValueError("MCP tool views must be a list")
    views: list[MCPServerToolView] = []
    for tool in raw_tools:
        if not isinstance(tool, Mapping):
            raise ValueError("MCP tool view must be an object")
        identity = str(tool.get("id") or "").strip()
        name = str(tool.get("name") or "").strip()
        if not identity or not name:
            raise ValueError("MCP tool view requires an id and a name")
        views.append(
            MCPServerToolView(
                id=identity,
                name=name,
                display_name=str(tool.get("display_name") or name).strip() or name,
            )
        )
    return views


def _preparation_phase(preparation: object, mcp_id: str, state: str) -> str:
    """What an in-flight preparation is doing, as one of the shared phases."""

    if state != MCP_STATE_PREPARING or preparation is None:
        return ""
    reader = getattr(preparation, "state", None)
    if not callable(reader):
        return ""
    prepared = reader(str(mcp_id))
    return "" if prepared is None else mcp_phase(getattr(prepared, "phase", ""))


async def _server_views(
    generation: PluginGeneration,
    snapshot: MCPHealthSnapshot,
    preparation: object | None = None,
) -> list[MCPServerView]:
    servers: list[MCPServerView] = []
    for mcp_id, mcp_runtime in generation.mcps.items():
        plugin_status = generation.statuses.get(mcp_runtime.runtime_id)
        status = str(
            snapshot.statuses.get(
                mcp_runtime.runtime_id,
                mcp_runtime.status,
            )
        )
        state = mcp_lifecycle_state(status)
        tools = _tool_views(mcp_runtime.tool_views)
        servers.append(
            MCPServerView(
                id=mcp_id,
                display_name=mcp_runtime.display_name,
                command=mcp_runtime.command,
                kind="mcp",
                status=status,
                state=state,
                phase=_preparation_phase(preparation, mcp_id, state),
                ready=mcp_state_ready(state),
                tools_count=len(tools),
                transport=mcp_runtime.transport,
                tools=tools,
                source_kind=mcp_runtime.source_kind,
                owner_plugin_id=mcp_runtime.owner_plugin_id,
                removable=mcp_runtime.owner_plugin_id is None and mcp_runtime.source_kind != "system",
                health_checked_at=snapshot.checked_at,
                detail=_status_detail(plugin_status),
            )
        )
    catalog = generation.capability_catalog
    if catalog is not None:
        active_ids = set(generation.mcps)
        for runtime_id, source in catalog.mcp_sources.items():
            if source.capability_id in active_ids:
                continue
            descriptor = _read_mcp_descriptor(source.manifest_path)
            plugin_status = generation.statuses.get(runtime_id)
            status = "not_loaded"
            if plugin_status is not None:
                status = plugin_status.health.status
            state = mcp_lifecycle_state(status)
            servers.append(MCPServerView(
                id=source.capability_id,
                display_name=descriptor["display_name"],
                command=descriptor["command"],
                kind="mcp",
                status=status,
                state=state,
                phase=_preparation_phase(preparation, source.capability_id, state),
                ready=mcp_state_ready(state),
                tools_count=0,
                transport=descriptor["transport"],
                tools=[],
                source_kind="system" if source.metadata.get("system_managed") else source.origin.value,
                owner_plugin_id=source.plugin_id,
                removable=source.plugin_id is None and not source.metadata.get("system_managed", False),
                health_checked_at=snapshot.checked_at,
                detail=_status_detail(plugin_status),
            ))
    return sorted(servers, key=lambda item: item.id)


#: The lifecycle projected back onto the raw status older consumers read.
#: ``status`` has to keep meaning what it meant, or a Console badge would go
#: gray on a server that failed to load.
_STATE_TO_STATUS = {
    MCP_STATE_NOT_LOADED: "not_loaded",
    MCP_STATE_PREPARING: "preparing",
    MCP_STATE_READY: "healthy",
    MCP_STATE_DEGRADED: "degraded",
    MCP_STATE_FAILED: "unhealthy",
    MCP_STATE_STOPPED: "stopped",
}


def _collapse(values: list[str]) -> str:
    """One value when every Worker agrees, ``degraded`` when they do not.

    A per-Agent Worker pool reports the same server several times; disagreement
    means one Agent loaded it and another did not, which is exactly what
    ``degraded`` is for.
    """

    distinct = set(values)
    return values[0] if len(distinct) == 1 else MCP_STATE_DEGRADED


def _warm_mcp_worker(runtime) -> None:
    """Make sure one Worker exists to prepare an MCP declaration.

    Only a Worker starts an MCP server, so on a device with no Worker running an
    install would stay a declaration until somebody happened to run an Expert --
    exactly the ``not_loaded`` the operator could not explain.  The Runtime
    already warms its device Worker during startup; an MCP mutation that finds
    none running asks for the same warm-up instead of waiting for one.
    """

    if getattr(runtime, "role", "worker") != "control":
        return
    supervisor = getattr(runtime, "worker_supervisor", None)
    if supervisor is None or getattr(supervisor, "ready", False):
        return
    warm = getattr(supervisor, "warm_agent", None)
    if not callable(warm):
        return
    from bianbu_agent.provisioning import system_starter_id

    starter = system_starter_id(runtime.config.data_dir)
    if starter:
        warm(starter)


async def _control_server_views(runtime) -> list[MCPServerView]:
    snapshot = control_catalog_snapshot(runtime)
    supervisor = getattr(runtime, "worker_supervisor", None)
    reports = []
    if callable(getattr(supervisor, "catalogs", None)):
        reports = await supervisor.catalogs()
    elif getattr(supervisor, "ready", False):
        try:
            async with asyncio.timeout(2.0):
                reports = [{"catalog": await supervisor.client.catalog()}]
        except Exception:
            reports = [{"error": "Worker catalog unavailable"}]
    views = []
    for item in snapshot.get("mcp_servers", []):
        view = dict(item)
        raw_states: list[str] = []
        lifecycle: list[str] = []
        phases: list[str] = []
        reasons: list[str] = []
        prepared_at: list[float] = []
        tools = {}
        for report in reports:
            if "error" in report:
                # 没有这行日志，Worker catalog 失败就只剩一个 unknown：
                # K3 上 StreamReader 64 KiB 上限把 112 KiB 的 catalog 打成
                # ValueError，Console 只显示「暂未发现工具」，日志里什么都没有。
                logger.warning(
                    "Worker catalog unavailable for MCP view: agent=%s error=%s",
                    report.get("agent_id"),
                    report["error"],
                )
                # One Worker that cannot answer is not a reason to call every
                # server healthy: it degrades the summary, like any other
                # disagreement between the pooled Workers.
                raw_states.append("unknown")
                lifecycle.append(MCP_STATE_UNKNOWN)
                continue
            catalog = report["catalog"]
            entry = catalog.get("mcp_statuses", {}).get(item["id"]) or {}
            # 失败原因由 Worker 从 generation 的 diagnostic 里取；丢了它，控制面上
            # 就只剩 unhealthy 三个字，用户无法知道该改什么。
            reason = str(entry.get("reason") or "").strip()
            if reason:
                reasons.append(reason)
            # 准备管理器失败的原因比对某台 Server 的诊断更贴近用户：它说的是
            # 「这个声明为什么起不来」，而不是某个 Generation 的看法。
            detail = str(entry.get("detail") or "").strip()
            if detail:
                reasons.append(detail)
            # 准备阶段是「preparing」唯一能解释自己的东西：装依赖和冷启动是两种
            # 不同的等待，只报 preparing 时用户盯着一动不动也不知道在等什么。
            phase = mcp_phase(entry.get("phase"))
            if phase:
                phases.append(phase)
            state = str(entry.get("state") or "").strip()
            if state:
                lifecycle.append(state)
            # 一个还没被任何 Expert 选中的 Server 只在准备报告里出现：工具有哪些、
            # 上次握手是什么时候，都只能从这里取，否则界面只剩一个 not_loaded。
            for tool in entry.get("tools") or []:
                if isinstance(tool, Mapping) and tool.get("id"):
                    tools.setdefault(str(tool["id"]), tool)
            stamp = entry.get("prepared_at")
            if isinstance(stamp, (int, float)) and float(stamp) > 0:
                prepared_at.append(float(stamp))
            loaded = next((server for server in catalog.get("mcp_servers", [])
                           if server["id"] == item["id"]), None)
            if loaded is not None:
                raw_states.append(str(loaded.get("status") or "unknown"))
                for tool in loaded.get("tools") or []:
                    if isinstance(tool, Mapping) and tool.get("id"):
                        tools.setdefault(str(tool["id"]), tool)
        if not reports:
            # No Worker answered at all: "not_loaded" would blame the server for
            # a catalog that was never read.
            lifecycle.append(MCP_STATE_UNKNOWN)
        state = _collapse(lifecycle) if lifecycle else mcp_lifecycle_state(view.get("status"))
        if raw_states:
            view["status"] = _collapse(raw_states)
        elif state in _STATE_TO_STATUS:
            # Nobody reported a raw status (the server never loaded) but a layer
            # did say what stage it reached; keep ``status`` in that vocabulary
            # so existing readers do not see a stale ``not_loaded``.
            view["status"] = _STATE_TO_STATUS[state]
        view["state"] = state
        view["phase"] = _collapse(phases) if state == MCP_STATE_PREPARING and phases else ""
        view["ready"] = mcp_state_ready(state)
        view["tools"] = _tool_views([tools[key] for key in sorted(tools)])
        view["tools_count"] = len(view["tools"])
        if prepared_at and not view.get("health_checked_at"):
            # 这台 Server 的工具来自 Runtime 自己的握手，不是来自一次健康检查：
            # 时间戳跟着那份证据走，界面才不会拿旧快照冒充当前状态。
            try:
                view["health_checked_at"] = datetime.fromtimestamp(
                    max(prepared_at), tz=timezone.utc
                )
            except (OverflowError, OSError, ValueError):
                # A record with an unusable timestamp is still a record: keep
                # its tools and leave the "checked at" empty.
                pass
        if reasons:
            view["detail"] = reasons[0]
        views.append(MCPServerView.model_validate(view))
    return sorted(views, key=lambda item: item.id)


async def _view_inputs(
    runtime,
    cache: MCPHealthCache,
    *,
    refresh: bool = False,
) -> tuple[PluginGeneration, MCPHealthSnapshot]:
    read_snapshot = cache.refresh if refresh else cache.read
    while True:
        snapshot = await read_snapshot()
        generation = runtime.plugin_runtime.current_generation
        if generation.id == snapshot.generation_id:
            return generation, snapshot


@router.get("", response_model=MCPServerCollectionEnvelope)
async def list_mcp_servers(request: Request) -> MCPServerCollectionEnvelope:
    runtime = request.app.state.runtime
    if getattr(runtime, "role", "worker") == "control":
        return MCPServerCollectionEnvelope(data=await _control_server_views(runtime))
    generation, snapshot = await _view_inputs(
        runtime,
        request.app.state.mcp_health_cache,
    )
    return MCPServerCollectionEnvelope(
        data=await _server_views(
            generation,
            snapshot,
            getattr(runtime, "mcp_preparation", None),
        )
    )


async def _require_empty_body(request: Request) -> None:
    if await request.body():
        raise HTTPException(
            422,
            {
                "code": "validation_error",
                "message": "MCP health checks do not accept a request body.",
                "details": {},
            },
        )


@health_router.post(
    "",
    response_model=MCPServerCollectionEnvelope,
    dependencies=[Depends(_require_empty_body)],
)
async def create_mcp_health_check(request: Request) -> MCPServerCollectionEnvelope:
    runtime = request.app.state.runtime
    if getattr(runtime, "role", "worker") == "control":
        _warm_mcp_worker(runtime)
        await _reload_active_worker(runtime, force=True)
        return MCPServerCollectionEnvelope(data=await _control_server_views(runtime))
    cache = request.app.state.mcp_health_cache
    reload_result = await _reload_active_worker(runtime, force=True)
    if bool(reload_result.get("changed")):
        await cache.invalidate()
    generation, snapshot = await _view_inputs(runtime, cache, refresh=True)
    return MCPServerCollectionEnvelope(
        data=await _server_views(
            generation,
            snapshot,
            getattr(runtime, "mcp_preparation", None),
        )
    )


@router.post("", status_code=201, response_model=SuccessEnvelope)
async def install_mcp_server(payload: MCPServerInstallRequest, request: Request):
    runtime = request.app.state.runtime
    # 安装分两段：先在锁内落下声明（文件系统与目录决策），再把声明发布给 Worker。
    # 发布要等 Worker 重建 generation，而它可能正忙着给某个 MCP 首次固化 npm 包
    # ——那是分钟级的事。锁陪着一起等，一次慢安装就会冻住后面所有 MCP 增删，
    # 新装的 Server 也永远不进列表（见 `_reload_active_worker_bounded`）。
    async with runtime.catalog_mutation_lock:
        definition, installed, backup, temporary = _install_mcp_declaration(payload, runtime)
    try:
        await _publish_mcp_install(runtime, definition, installed, backup)
    finally:
        temporary.cleanup()
    await request.app.state.mcp_health_cache.invalidate()
    if getattr(runtime, "role", "worker") == "control":
        views = await _control_server_views(runtime)
    else:
        generation, snapshot = await _view_inputs(
            runtime,
            request.app.state.mcp_health_cache,
        )
        views = await _server_views(
            generation,
            snapshot,
            getattr(runtime, "mcp_preparation", None),
        )
    installed_view = next(
        (item for item in views if item.id == definition.plugin_id),
        None,
    )
    if installed_view is None:
        # 装上了却不在目录里，调用方除了干等无事可做：说清楚比 500 有用。
        raise _conflict(
            "mcp_install_not_visible",
            _mutation_message(definition.plugin_id, "installed"),
            {"mcp_id": definition.plugin_id},
        )
    _warm_mcp_worker(runtime)
    return success(installed_view, status_code=201)


def _install_mcp_declaration(payload, runtime):
    """Write the declaration. The caller holds ``catalog_mutation_lock``."""

    definition = parse_managed_mcp(payload.name, payload.command)
    previous = runtime.mcps.inspect(definition.plugin_id)
    if previous is not None and not payload.upgrade:
        raise _conflict(
            "mcp_already_installed",
            f"MCP server {definition.plugin_id} is already installed",
            {"mcp_id": definition.plugin_id},
        )
    temporary = tempfile.TemporaryDirectory(prefix="bianbu-managed-mcp-")
    try:
        backup = None
        if previous is not None and previous.root.parent == runtime.home.mcps_dir:
            backup = Path(temporary.name) / "previous" / previous.id
            shutil.copytree(previous.root, backup)
        # 安装只登记声明：npm 固化与首次启动由 Worker 在后台准备完成（见
        # MCPPreparationManager），任何调用方都不必等一个可能几分钟的下载。
        package = write_managed_mcp_package(definition, Path(temporary.name))
        try:
            installed = runtime.mcps.install(package, upgrade=payload.upgrade)
        except FileExistsError as exc:
            raise _conflict(
                "mcp_already_installed",
                f"MCP server {definition.plugin_id} is already installed",
                {"mcp_id": definition.plugin_id},
            ) from exc
    except BaseException:
        temporary.cleanup()
        raise
    return definition, installed, backup, temporary


async def _publish_mcp_install(runtime, definition, installed, backup: Path | None) -> None:
    """Publish a staged install; roll the declaration back if the Worker refuses."""

    try:
        await _reload_active_worker_bounded(runtime)
    except HTTPException:
        # 回滚自己失败也不能顶掉 Worker-unavailable：调用方要按状态码决定重试。
        try:
            await _rollback_mcp_install(runtime, installed, backup)
        except Exception as rollback_exc:  # noqa: BLE001
            logger.error(
                "MCP server %s activation rollback failed (%s)",
                definition.plugin_id,
                type(rollback_exc).__name__,
            )
        raise
    except Exception as exc:
        rolled_back = False
        try:
            rolled_back = await _rollback_mcp_install(runtime, installed, backup)
        except Exception as rollback_exc:  # noqa: BLE001
            logger.error(
                "MCP server %s activation rollback failed (%s)",
                definition.plugin_id,
                type(rollback_exc).__name__,
            )
        logger.error(
            "MCP server %s activation failed (%s); rolled_back=%s",
            definition.plugin_id,
            type(exc).__name__,
            rolled_back,
        )
        raise _conflict(
            "mcp_activation_failed",
            _mutation_message(definition.plugin_id, "activated"),
            {
                "mcp_id": definition.plugin_id,
                "error_type": type(exc).__name__,
                "rolled_back": rolled_back,
            },
        ) from exc
    async with runtime.catalog_mutation_lock:
        _cleanup_retired_if_idle(runtime)


async def _rollback_mcp_install(runtime, installed, backup: Path | None) -> bool:
    """Undo a staged install. The publisher runs unlocked, so take the lock back."""

    async with runtime.catalog_mutation_lock:
        return _discard_mcp_install(runtime, installed, backup)


def _discard_mcp_install(runtime, installed, backup: Path | None) -> bool:
    removed = runtime.mcps.discard_install(installed)
    if backup is not None:
        runtime.mcps.install(backup)
    return removed


@router.delete("", status_code=204)
async def delete_mcp_server(
    request: Request,
    id: str = Query(min_length=1, max_length=512),
) -> Response:
    runtime = request.app.state.runtime
    mcp_id = id.strip()
    # 与安装同一条纪律：锁只负责目录与文件系统决策，发布给 Worker 放在锁外，
    # 免得一个正忙的 Worker 把后面所有 MCP 增删一起冻住。
    async with runtime.catalog_mutation_lock:
        backup, temporary = await _remove_mcp_declaration(runtime, mcp_id)
    try:
        await _publish_mcp_removal(runtime, mcp_id, backup)
    finally:
        temporary.cleanup()
    await request.app.state.mcp_health_cache.invalidate()
    return Response(status_code=204)


async def _remove_mcp_declaration(runtime, mcp_id: str):
    """Remove the declaration. The caller holds ``catalog_mutation_lock``."""

    if getattr(runtime, "role", "worker") == "control":
        active = next(
            (item for item in await _control_server_views(runtime) if item.id == mcp_id),
            None,
        )
        owner_plugin_id = None if active is None else active.owner_plugin_id
    else:
        active = runtime.plugin_runtime.current_generation.mcps.get(mcp_id)
        owner_plugin_id = None if active is None else active.owner_plugin_id
    if owner_plugin_id is not None:
        raise _conflict(
            "mcp_owned_by_plugin",
            (
                f"MCP server {mcp_id} is managed by Plugin "
                f"{owner_plugin_id}; remove the owning Plugin instead"
            ),
            {
                "mcp_id": mcp_id,
                "owner_plugin_id": owner_plugin_id,
            },
        )
    if active is not None and active.source_kind == "system":
        raise _conflict("mcp_managed_by_system",
                        f"MCP server {mcp_id} is managed by the system package manager",
                        {"mcp_id": mcp_id})
    # Agent manifests intentionally retain missing selectors. The catalog
    # removes this server immediately and availability projection explains the
    # stale reference until the user edits the Agent or reinstalls the MCP.
    try:
        installed = runtime.mcps.inspect(mcp_id)
    except ValueError:
        installed = None
    if installed is None:
        raise KeyError(mcp_id)
    try:
        runtime.mcps.assert_deletable(mcp_id)
    except ValueError as exc:
        raise _conflict("mcp_managed_by_system",
                        f"MCP server {mcp_id} is managed by the system package manager",
                        {"mcp_id": mcp_id}) from exc
    temporary = tempfile.TemporaryDirectory(prefix="bianbu-mcp-rollback-")
    try:
        backup = Path(temporary.name) / installed.id
        shutil.copytree(installed.root, backup)
        if not runtime.mcps.uninstall(mcp_id):
            raise KeyError(mcp_id)
    except BaseException:
        temporary.cleanup()
        raise
    return backup, temporary


async def _publish_mcp_removal(runtime, mcp_id: str, backup: Path) -> None:
    """Publish a removal; restore the declaration if the Worker refuses it."""

    try:
        await _reload_active_worker_bounded(runtime)
    except HTTPException:
        try:
            await _restore_mcp_declaration(runtime, backup)
        except Exception as rollback_exc:  # noqa: BLE001
            logger.error(
                "MCP server %s removal rollback failed (%s)",
                mcp_id,
                type(rollback_exc).__name__,
            )
        raise
    except Exception as exc:
        rolled_back = False
        try:
            await _restore_mcp_declaration(runtime, backup)
            rolled_back = True
        except Exception as rollback_exc:  # noqa: BLE001
            logger.error(
                "MCP server %s removal rollback failed (%s)",
                mcp_id,
                type(rollback_exc).__name__,
            )
        logger.error(
            "MCP server %s removal reload failed (%s); rolled_back=%s",
            mcp_id,
            type(exc).__name__,
            rolled_back,
        )
        raise _conflict(
            "mcp_reload_failed",
            _mutation_message(mcp_id, "removed"),
            {
                "mcp_id": mcp_id,
                "error_type": type(exc).__name__,
                "rolled_back": rolled_back,
            },
        ) from exc
    async with runtime.catalog_mutation_lock:
        remove_vendored_server(runtime.home.mcp_runtime_dir / mcp_id)
        _cleanup_retired_if_idle(runtime)


async def _restore_mcp_declaration(runtime, backup: Path) -> None:
    """Put a backed-up declaration back. The publisher runs unlocked."""

    async with runtime.catalog_mutation_lock:
        runtime.mcps.install(backup)
    await _reload_active_worker_bounded(runtime)


def _conflict(
    code: str,
    message: str,
    details: Mapping[str, object],
) -> HTTPException:
    return HTTPException(
        409,
        {"code": code, "message": message, "details": dict(details)},
    )


def _mutation_message(mcp_id: str, action: str) -> str:
    return f"MCP server {mcp_id} could not be {action} in the active runtime."


__all__ = ["health_router", "router"]
