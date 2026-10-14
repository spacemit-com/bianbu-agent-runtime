"""Typed administration resources for packages and Plugins."""

from __future__ import annotations

from collections.abc import Mapping
import asyncio
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import cast

from fastapi import APIRouter, HTTPException, Request, Response

from bianbu_agent.agents import AgentManifest, agent_model_data
from bianbu_agent.api.contracts import PackageInstallRequest, PackageKind, PluginCreateRequest
from bianbu_agent.api.errors import success
from bianbu_agent.api.catalog_dependencies import (
    agent_references,
    capability_in_use,
)
from bianbu_agent.plugins.models import InstalledPlugin, PluginMember
from bianbu_agent.server.runtime import BianbuAgentRuntime
from bianbu_agent.security.runtime_process.worker_rpc import WorkerRPCError


router = APIRouter()
logger = logging.getLogger(__name__)


class _WorkerReloadRejected(RuntimeError):
    """Worker was reachable but refused to publish a candidate generation."""


def _runtime(request: Request) -> BianbuAgentRuntime:
    return cast(BianbuAgentRuntime, request.app.state.runtime)


def _manager(runtime: BianbuAgentRuntime, kind: PackageKind):
    managers = {
        PackageKind.SKILL: runtime.skills,
        PackageKind.AGENT: runtime.agent_packages,
    }
    return managers[kind]


def _source(value: str) -> Path:
    source = Path(value).expanduser()
    if source.is_symlink():
        raise ValueError(f"symbolic links are not allowed: {source}")
    if not source.is_absolute() or not source.exists():
        raise ValueError("source must be an existing absolute local path")
    return source


async def _reload_active_worker(
    runtime: BianbuAgentRuntime,
    *,
    force: bool = False,
) -> dict[str, object]:
    """Publish installed package changes in the isolated Worker.

    The control plane owns package files and metadata, while the Worker owns
    executable generations.  Never call ``reload_installed`` on the control
    facade: it is intentionally unable to load third-party code.
    """
    if runtime.role != "control":
        result = (
            await runtime.plugin_runtime.reload_installed(force=True)
            if force
            else await runtime.plugin_runtime.reload_installed()
        )
        return {"generation_id": int(result.generation_id), "changed": bool(result.changed)}
    supervisor = getattr(runtime, "worker_supervisor", None)
    if supervisor is None:
        # Installation state belongs to Control. A Worker created later reads
        # the current inventory during startup, so an empty pool is already in
        # sync and must not make a catalog mutation fail.
        return {"generation_id": None, "changed": False, "worker_count": 0}
    try:
        reload_all = getattr(supervisor, "reload_installed", None)
        if callable(reload_all):
            return dict(await reload_all(force=force))
        if not supervisor.ready:
            await supervisor.start()
        client = supervisor.client
        if client is None:
            raise RuntimeError("Worker RPC client is not ready")
        response = await client.reload(force=True) if force else await client.reload()
        return dict(response)
    except WorkerRPCError as exc:
        # The Worker was reachable but rejected the candidate generation. Let
        # the caller's existing 409 rollback path preserve that distinction.
        raise _WorkerReloadRejected(str(exc)) from exc
    except _WorkerReloadRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - RPC boundary
        logger.warning(
            "Activating a package change failed: error=%s", exc, exc_info=exc
        )
        raise HTTPException(503, {
            "code": "worker_unavailable",
            "message": (
                "Agent Worker is not available; package change was not "
                f"activated: {exc}"
            ),
        }) from exc


#: The Worker publishes a change by loading every affected package, and the first
#: load of an ``npx`` MCP Server materialises its npm package first. On a board
#: that is minutes of work, and a mutation that waits for it holds
#: ``catalog_mutation_lock`` the whole time: one slow install then freezes every
#: later mutation and the added resource never shows up in any list. The
#: declaration on disk is already the catalog's source of truth, so a mutation
#: may report the change before the Worker has published it.
WORKER_ACTIVATION_TIMEOUT_SECONDS = 5.0

#: Reloads that outlived the caller's patience. Referenced here so asyncio does
#: not collect them mid-flight; ``_activation_finished`` drains them.
_pending_activations: set[asyncio.Task[object]] = set()


def _activation_finished(task: asyncio.Task[object]) -> None:
    _pending_activations.discard(task)
    if not task.cancelled():
        # 取走异常：超时之后没人再 await 这个 task，否则 asyncio 会打
        # never-retrieved 告警。
        task.exception()


async def _reload_active_worker_bounded(
    runtime: BianbuAgentRuntime,
    *,
    force: bool = False,
    timeout: float | None = None,
) -> dict[str, object] | None:
    """Wait for the Worker to publish a change, but never forever.

    ``None`` means the Worker had not answered within ``timeout``. The reload
    keeps running in the background so the change is still published once the
    Worker is free; callers report the mutation from the catalog they own.
    """
    if timeout is None:
        timeout = WORKER_ACTIVATION_TIMEOUT_SECONDS
    activation = asyncio.create_task(_reload_active_worker(runtime, force=force))
    _pending_activations.add(activation)
    activation.add_done_callback(_activation_finished)
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.shield(activation)
    except TimeoutError:
        logger.warning(
            "Worker activation is still running after %.0fs; reporting the change "
            "from the catalog and letting the Worker publish it when it is free",
            timeout,
        )
        return None


def _cleanup_retired_if_idle(runtime: BianbuAgentRuntime) -> None:
    cleanup = getattr(runtime, "cleanup_retired_packages", None)
    if callable(cleanup):
        cleanup()


def _package_view(kind: PackageKind, package: object, *, security_status: str | None = None) -> dict[str, object]:
    package_id = (
        str(getattr(package, "id"))
        if kind is PackageKind.AGENT
        else str(getattr(package, "capability_id"))
    )
    view: dict[str, object] = {
        "id": package_id,
        "kind": kind.value,
        "version": str(getattr(package, "version")),
        "content_digest": str(getattr(package, "content_digest")),
    }
    if kind is PackageKind.AGENT:
        spec = getattr(getattr(package, "manifest"), "spec")
        view["agent"] = {
            "id": spec.id,
            "name": spec.name,
            "name_zh": spec.name_zh,
            "name_en": spec.name_en,
            "description": spec.description,
            "description_zh": spec.description_zh,
            "soul": spec.soul,
            "model": agent_model_data(spec.model),
            "tools": list(spec.tools),
            "skills": list(spec.skills),
            "mcps": list(spec.mcps),
            "metadata": dict(spec.metadata),
        }
        if security_status is not None:
            view["security_status"] = security_status
    return view


@router.get("/packages/{kind}")
async def list_packages(kind: PackageKind, request: Request):
    runtime = _runtime(request)
    manager = _manager(runtime, kind)
    pending = runtime.security_pending
    return success([
        _package_view(
            kind,
            item,
            security_status=(
                "security_pending" if kind is PackageKind.AGENT and str(getattr(item, "id", "")) in pending else ("registered" if kind is PackageKind.AGENT else None)
            ),
        )
        for item in manager.list()
    ])


@router.post("/packages/{kind}", status_code=201)
async def install_package(
    kind: PackageKind,
    payload: PackageInstallRequest,
    request: Request,
):
    runtime = _runtime(request)
    manager = _manager(runtime, kind)
    async with runtime.catalog_mutation_lock:
        agent_backup: Path | None = None
        previous = None
        skill_backup: Path | None = None
        backup_tmp = None
        if kind is PackageKind.SKILL and payload.upgrade:
            from bianbu_agent.skills.loader import DirectorySkillLoader
            skill_id = DirectorySkillLoader().load(_source(payload.source)).spec.name
            user_source = runtime.home.skills_dir / skill_id
            if user_source.is_dir() and not user_source.is_symlink():
                backup_tmp = tempfile.TemporaryDirectory(prefix="bianbu-skill-upgrade-")
                skill_backup = Path(backup_tmp.name) / skill_id
                from bianbu_agent.plugins.ownership import OWNERSHIP_FILENAME
                shutil.copytree(user_source, skill_backup, ignore=shutil.ignore_patterns(OWNERSHIP_FILENAME))
        if kind is PackageKind.AGENT and payload.upgrade:
            try:
                manifest = AgentManifest.from_path(Path(payload.source).expanduser() / "agent.yaml")
                previous = manager.inspect(manifest.spec.id)
                if previous is not None:
                    backup_tmp = tempfile.TemporaryDirectory(prefix="bianbu-agent-upgrade-")
                    agent_backup = Path(backup_tmp.name) / "previous"
                    shutil.copytree(previous.root, agent_backup)
            except (FileNotFoundError, OSError, ValueError):
                pass
        try:
            if kind is PackageKind.AGENT:
                installed = manager.install(_source(payload.source), upgrade=payload.upgrade)
            elif kind is PackageKind.SKILL:
                installed = manager.install(
                    _source(payload.source),
                    upgrade=payload.upgrade,
                )
            else:
                installed = manager.install(_source(payload.source))
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        security_status = "not_configured"
        if kind is PackageKind.AGENT:
            try:
                await runtime.register_logical_agent(installed.manifest.spec)
                security_status = "registered"
            except Exception as exc:  # noqa: BLE001 - security lifecycle boundary
                if runtime.config.security.required and str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production":
                    try:
                        manager._rollback_security_install(installed, previous, agent_backup)
                    finally:
                        logger.warning(
                            "Agent security registration failed: agent=%s error=%s",
                            installed.id, exc, exc_info=exc,
                        )
                        raise HTTPException(503, {
                            "code": "agent_security_registration_failed",
                            "message": (
                                "Agent security registration failed: "
                                f"{exc}"
                            ),
                        }) from exc
                runtime._security_pending[installed.id] = str(exc)
                security_status = "security_pending"
        if kind is not PackageKind.AGENT:
            try:
                await _reload_active_worker(runtime)
            except HTTPException:
                package_id = str(installed.capability_id)
                try:
                    _rollback_package_install(manager, kind, installed, skill_backup)
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.error(
                        "%s package %s Worker-unavailable rollback failed (%s)",
                        kind.value,
                        package_id,
                        type(rollback_exc).__name__,
                    )
                raise
            except Exception as exc:
                package_id = str(installed.capability_id)
                rolled_back = False
                try:
                    rolled_back = _rollback_package_install(manager, kind, installed, skill_backup)
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.error(
                        "%s package %s activation rollback failed (%s)",
                        kind.value,
                        package_id,
                        type(rollback_exc).__name__,
                    )
                raise _package_conflict(
                    "package_activation_failed",
                    kind,
                    package_id,
                    "activated",
                    exc,
                    rolled_back,
                ) from exc
            _cleanup_retired_if_idle(runtime)
    if backup_tmp is not None:
        backup_tmp.cleanup()
    view = _package_view(kind, installed, security_status=security_status if kind is PackageKind.AGENT else None)
    return success(view, status_code=201)


@router.get("/packages/{kind}/{package_id}")
async def inspect_package(kind: PackageKind, package_id: str, request: Request):
    runtime = _runtime(request)
    item = _manager(runtime, kind).inspect(package_id)
    if item is None:
        raise KeyError(package_id)
    view = _package_view(
        kind,
        item,
        security_status=(
            ("security_pending" if package_id in runtime.security_pending else "registered")
            if kind is PackageKind.AGENT else None
        ),
    )
    return success(view)


@router.post("/packages/{kind}/{package_id}/verify")
async def verify_package(kind: PackageKind, package_id: str, request: Request):
    manager = _manager(_runtime(request), kind)
    if manager.inspect(package_id) is None:
        raise KeyError(package_id)
    return success({"valid": bool(manager.verify(package_id))})


@router.delete("/packages/{kind}/{package_id}", status_code=204)
async def delete_package(
    kind: PackageKind,
    package_id: str,
    request: Request,
) -> Response:
    runtime = _runtime(request)
    manager = _manager(runtime, kind)
    async with runtime.catalog_mutation_lock:
        if manager.inspect(package_id) is None:
            raise KeyError(package_id)
        if kind not in {PackageKind.AGENT, PackageKind.SKILL}:
            references = await agent_references(
                runtime,
                {kind.value: (package_id,)},
            )
            if references:
                raise capability_in_use(kind.value, package_id, references)
        if kind in {PackageKind.AGENT, PackageKind.SKILL}:
            manager.assert_deletable(package_id)
        if kind is PackageKind.AGENT:
            try:
                await runtime.unregister_logical_agent(package_id)
            except Exception as exc:
                if runtime.config.security.required and str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production":
                    logger.warning(
                        "Agent security unregister failed: agent=%s error=%s",
                        package_id, exc, exc_info=exc,
                    )
                    raise HTTPException(503, {
                        "code": "agent_security_unregister_failed",
                        "message": (
                            "Agent security unregister failed: " f"{exc}"
                        ),
                    }) from exc
            manager.uninstall(package_id)
            return Response(status_code=204)
        root = _package_root(runtime, kind, package_id)
        with tempfile.TemporaryDirectory(prefix="bianbu-package-rollback-") as temporary:
            backup = Path(temporary) / package_id
            shutil.copytree(root, backup)
            deleted = _uninstall_package(manager, kind, package_id)
            try:
                await _reload_active_worker(runtime)
            except HTTPException:
                try:
                    manager.install(backup)
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.error(
                        "%s package %s Worker-unavailable rollback failed (%s)",
                        kind.value,
                        package_id,
                        type(rollback_exc).__name__,
                    )
                raise
            except Exception as exc:
                rolled_back = False
                try:
                    manager.install(backup)
                    await _reload_active_worker(runtime)
                    rolled_back = True
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.error(
                        "%s package %s rollback failed (%s)",
                        kind.value,
                        package_id,
                        type(rollback_exc).__name__,
                    )
                raise _package_conflict(
                    "package_reload_failed",
                    kind,
                    package_id,
                    "removed",
                    exc,
                    rolled_back,
                ) from exc
            if not deleted:
                raise KeyError(package_id)
            _cleanup_retired_if_idle(runtime)
    return Response(status_code=204)


def _package_root(runtime, kind: PackageKind, package_id: str) -> Path:
    roots = {
        PackageKind.SKILL: runtime.home.skills_dir,
    }
    return roots[kind] / package_id


def _rollback_package_install(manager, kind: PackageKind, installed, skill_backup: Path | None) -> bool:
    if kind is PackageKind.SKILL:
        removed = manager.discard_install(installed)
        if skill_backup is not None:
            manager.install(skill_backup)
        return removed
    return _uninstall_package(manager, kind, str(installed.capability_id))


def _uninstall_package(manager, kind: PackageKind, package_id: str) -> bool:
    return bool(manager.uninstall(package_id))


def _package_conflict(
    code: str,
    kind: PackageKind,
    package_id: str,
    action: str,
    error: Exception,
    rolled_back: bool,
) -> HTTPException:
    return HTTPException(
        409,
        {
            "code": code,
            "message": (
                f"{kind.value.title()} package {package_id} could not be "
                f"{action} in the active runtime."
            ),
            "details": {
                "kind": kind.value,
                "capability_id": package_id,
                "error_type": type(error).__name__,
                "rolled_back": rolled_back,
            },
        },
    )


def _member_view(member: PluginMember) -> dict[str, object]:
    return {
        "id": member.qualified_id,
        "plugin_id": member.namespace,
        "kind": member.kind,
        "name": member.local_name,
        "version": member.version,
    }


def _plugin_view(plugin: InstalledPlugin) -> dict[str, object]:
    return {
        "id": plugin.id,
        "version": plugin.manifest.version,
        "description": plugin.manifest.description,
        "content_digest": plugin.content_digest,
        "members": [_member_view(item) for item in plugin.members],
    }


@router.get("/plugins")
async def list_plugins(request: Request):
    return success([_plugin_view(item) for item in _runtime(request).plugins.list()])


@router.post("/plugins", status_code=201)
async def create_plugin(payload: PluginCreateRequest, request: Request):
    runtime = _runtime(request)
    async with runtime.catalog_mutation_lock:
        user_plugin_ids = {
            plugin.id for plugin in runtime.plugins.list()
            if runtime.plugins.package_root(plugin.id).parent == runtime.home.plugins_dir
        }
        try:
            item = runtime.plugins.install(
                _source(payload.source), upgrade=payload.upgrade
            )
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        try:
            await _reload_active_worker(runtime)
        except HTTPException:
            # Installation is the privileged approval boundary. If no Worker
            # generation accepts it, remove the approved package atomically.
            try:
                if item.id in user_plugin_ids:
                    runtime.plugins.rollback(item.id)
                else:
                    runtime.plugins.discard_install(item)
            except Exception as rollback_exc:  # noqa: BLE001
                logger.error("Plugin %s Worker-unavailable rollback failed (%s)", item.id, type(rollback_exc).__name__)
            raise
        except Exception as exc:
            rolled_back = False
            try:
                if item.id in user_plugin_ids:
                    runtime.plugins.rollback(item.id)
                    rolled_back = True
                else:
                    rolled_back = runtime.plugins.discard_install(item)
            except Exception as rollback_exc:
                logger.error(
                    "Plugin %s activation rollback failed (%s)",
                    item.id,
                    type(rollback_exc).__name__,
                )
            logger.error(
                "Plugin %s activation failed (%s); rolled_back=%s",
                item.id,
                type(exc).__name__,
                rolled_back,
            )
            raise _plugin_conflict(
                "plugin_activation_failed",
                _plugin_mutation_message(item.id, "activated"),
                {
                    "plugin_id": item.id,
                    "error_type": type(exc).__name__,
                    "rolled_back": rolled_back,
                },
            ) from exc
    return success(_plugin_view(item), status_code=201)


@router.post("/plugins/{plugin_id}/rollback")
async def rollback_plugin(plugin_id: str, request: Request):
    runtime = _runtime(request)
    async with runtime.catalog_mutation_lock:
        restored = runtime.plugins.rollback(plugin_id)
        try:
            await _reload_active_worker(runtime)
        except Exception:
            try:
                runtime.plugins.rollback(plugin_id)
                await _reload_active_worker(runtime)
            except Exception as rollback_exc:  # noqa: BLE001
                logger.error(
                    "Plugin %s rollback recovery failed (%s)",
                    plugin_id,
                    type(rollback_exc).__name__,
                )
            raise
    return success(_plugin_view(restored))


@router.get("/plugins/effective")
async def effective_plugins(request: Request):
    runtime = _runtime(request)
    if runtime.role == "control":
        return success(runtime.plugins.effective(runtime.config.plugin_configs))
    generation = runtime.plugin_runtime.current_generation
    installed = {item.id: item for item in runtime.plugins.list()}
    views: list[dict[str, object]] = []
    dependency_order = {
        plugin_id: order for order, plugin_id in enumerate(generation.dependency_order)
    }
    for plugin_id, plugin_runtime in sorted(generation.plugins.items()):
        manifest = plugin_runtime.instance.manifest
        status = generation.statuses.get(plugin_id)
        contributions = plugin_runtime.contributions
        views.append(
            {
                "id": plugin_id,
                "version": manifest.version,
                "source": (
                    str(runtime.plugins.package_root(plugin_id))
                    if plugin_id in installed
                    else "runtime"
                ),
                "generation_id": generation.id,
                "state": status.state.value if status is not None else "unknown",
                "enabled": True,
                "dependency_order": dependency_order.get(plugin_id),
                "dependencies": [
                    {"id": dependency.plugin_id, "version": dependency.version}
                    for dependency in manifest.dependencies
                ],
                "config": dict(plugin_runtime.config),
                "provenance": dict(plugin_runtime.config_provenance),
                "members": sorted(
                    [
                        str(item.spec.metadata.get("qualified_id") or item.spec.name)
                        for item in contributions.tools
                    ]
                    + [
                        str(item.spec.metadata.get("qualified_id") or item.spec.name)
                        for item in contributions.skills
                    ]
                    + [
                        str(item.metadata.get("qualified_id") or f"{plugin_id}/{item.id}")
                        for item in contributions.hooks
                    ]
                    + [
                        mcp_id
                        for mcp_id, mcp in generation.mcps.items()
                        if mcp.owner_plugin_id == plugin_id
                    ]
                ),
                "contributions": {
                    "tools": sorted(
                        str(item.spec.metadata.get("qualified_id") or item.spec.name)
                        for item in contributions.tools
                    ),
                    "skills": sorted(
                        str(item.spec.metadata.get("qualified_id") or item.spec.name)
                        for item in contributions.skills
                    ),
                    "hooks": sorted(
                        str(item.metadata.get("qualified_id") or f"{plugin_id}/{item.id}")
                        for item in contributions.hooks
                    ),
                    "mcps": sorted(
                        mcp_id
                        for mcp_id, mcp in generation.mcps.items()
                        if mcp.owner_plugin_id == plugin_id
                    ),
                },
            }
        )
    return success(views)


@router.get("/plugins/{plugin_id}")
async def inspect_plugin(plugin_id: str, request: Request):
    item = _runtime(request).plugins.inspect(plugin_id)
    if item is None:
        raise KeyError(plugin_id)
    return success(_plugin_view(item))


@router.delete("/plugins/{plugin_id}", status_code=204)
async def delete_plugin(plugin_id: str, request: Request) -> Response:
    runtime = _runtime(request)
    manager = runtime.plugins
    async with runtime.catalog_mutation_lock:
        installed = manager.inspect(plugin_id)
        if installed is None:
            raise KeyError(plugin_id)
        selections: dict[str, list[str]] = {"plugin": [plugin_id]}
        for member in installed.members:
            selections.setdefault(str(member.kind), []).append(member.qualified_id)
        references = await agent_references(runtime, selections)
        if references:
            raise capability_in_use("plugin", plugin_id, references)
        return await _delete_plugin_locked(runtime, installed)


async def _delete_plugin_locked(runtime, installed: InstalledPlugin) -> Response:
    plugin_id = installed.id
    manager = runtime.plugins
    try:
        manager.assert_deletable(plugin_id)
    except ValueError as exc:
        # ``ValueError`` renders as a 422 ``validation_error`` carrying the
        # manager's developer-facing English ("system Plugin cannot be
        # uninstalled"), which a console user cannot act on.  This is a policy
        # refusal, not malformed input.
        raise _plugin_conflict(
            "plugin_managed_by_system",
            (
                f"插件 {plugin_id} 由系统包管理，随 bianbu-agent-runtime 一起安装，"
                "无法在这里卸载。"
            ),
            {"plugin_id": plugin_id},
        ) from exc
    with tempfile.TemporaryDirectory(prefix="bianbu-plugin-rollback-") as temporary:
        backup = Path(temporary) / installed.id
        shutil.copytree(runtime.home.plugins_dir / installed.id, backup)
        manager.uninstall(plugin_id)
        try:
            await _reload_active_worker(runtime)
        except HTTPException:
            try:
                manager.install(backup)
            except Exception as rollback_exc:  # noqa: BLE001
                logger.error("Plugin %s Worker-unavailable rollback failed (%s)", plugin_id, type(rollback_exc).__name__)
            raise
        except Exception as exc:
            rolled_back = False
            try:
                manager.install(backup)
                await _reload_active_worker(runtime)
                rolled_back = True
            except Exception as rollback_exc:
                logger.error(
                    "Plugin %s removal rollback failed (%s)",
                    plugin_id,
                    type(rollback_exc).__name__,
                )
            logger.error(
                "Plugin %s removal reload failed (%s); rolled_back=%s",
                plugin_id,
                type(exc).__name__,
                rolled_back,
            )
            raise _plugin_conflict(
                "plugin_reload_failed",
                _plugin_mutation_message(plugin_id, "removed"),
                {
                    "plugin_id": plugin_id,
                    "error_type": type(exc).__name__,
                    "rolled_back": rolled_back,
                },
            ) from exc
    return Response(status_code=204)


def _plugin_conflict(
    code: str,
    message: str,
    details: Mapping[str, object],
) -> HTTPException:
    return HTTPException(
        409,
        {"code": code, "message": message, "details": dict(details)},
    )


def _plugin_mutation_message(
    plugin_id: str,
    action: str,
) -> str:
    return f"Plugin {plugin_id} could not be {action} in the active runtime."


__all__ = ["router"]
