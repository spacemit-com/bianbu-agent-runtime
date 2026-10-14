"""Agent resource views and routes for the versioned HTTP API."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from bianbu_agent.agents import AgentModel, AgentSpec, agent_model_data
from bianbu_agent.agents.manager import is_mcp_tool
from bianbu_agent.mcp import resolve_mcp_selectors
from bianbu_agent.api.contracts import (
    AgentCollectionEnvelope,
    AgentCreateRequest,
    AgentEnvelope,
    AgentUpdateRequest,
    AgentView,
)
from bianbu_agent.api.capabilities import control_catalog_snapshot
from bianbu_agent.errors import ResourceNotFoundError
from bianbu_agent.provisioning import new_agent_id


router = APIRouter(prefix="/agents")

_AVATAR_LIMIT = 2 * 1024 * 1024
_AVATAR_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_AVATAR_MEDIA = {suffix: media for media, suffix in _AVATAR_SUFFIX.items()}
_AVATAR_BODY = {
    "required": True,
    "content": {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary"}},
                "additionalProperties": False,
            }
        }
    },
}


def agent_view(
    spec: AgentSpec,
    *,
    providers: Mapping[str, object] | None = None,
    generation: object | None = None,
    avatar_url: str | None = None,
    system: bool = False,
    execution: dict | None = None,
) -> AgentView:
    """Serialize an Agent definition without depending on a browser facade."""

    metadata = dict(spec.metadata)
    return AgentView(
        id=spec.id,
        origin="system" if system else "user",
        execution=execution or {},
        name=spec.name,
        name_zh=spec.name_zh,
        name_en=spec.name_en,
        avatar=avatar_url or str(metadata.get("avatar") or "🤖"),
        description=spec.description,
        description_zh=spec.description_zh,
        soul=spec.soul,
        model=agent_model_data(spec.model),
        tools=list(spec.tools),
        skills=list(spec.skills),
        # Report the MCP IDs this device installed, so an editor selects the
        # server instead of keeping the APM name as a second, missing entry.
        mcps=list(
            resolve_mcp_selectors(spec.mcps, getattr(generation, "mcps", {}) or {})
            if generation is not None
            else spec.mcps
        ),
        plugins=list(spec.plugins),
        permissions=dict(spec.permissions),
        metadata=metadata,
        **_availability(spec, providers or {}, generation),
    )


def _availability(
    spec: AgentSpec,
    providers: Mapping[str, object],
    generation: object | None = None,
) -> dict[str, object]:
    missing_powers, tools_missing = _capability_state(spec, generation)
    if tools_missing:
        return {
            "available": False,
            "unavailable_reason": "agent_tool_not_found",
            "missing_powers": missing_powers,
        }
    if not isinstance(spec.model, AgentModel):
        return {
            "available": False,
            "unavailable_reason": "agent_model_required",
            "missing_powers": missing_powers,
        }
    provider = providers.get(spec.model.provider)
    if provider is None and spec.model.provider.startswith("custom:"):
        provider = providers.get(
            f"custom-provider-{spec.model.provider.split(':', 1)[1]}"
        )
    if provider is None:
        return {
            "available": False,
            "unavailable_reason": "agent_provider_not_found",
            "missing_powers": missing_powers,
        }
    if not bool(getattr(provider, "configured", False)):
        return {
            "available": False,
            "unavailable_reason": "agent_provider_incomplete",
            "missing_powers": missing_powers,
        }
    return {"available": True, "unavailable_reason": "", "missing_powers": missing_powers}


def _capability_state(
    spec: AgentSpec,
    generation: object | None,
) -> tuple[list[str], bool]:
    """Report declared-but-missing Powers and whether a Tool gap is fatal.

    An Agent names built-in Tools only; a Tool an MCP contributes is selected by
    naming the server under ``mcps``.  A missing Tool keeps the Agent
    unavailable because the model may not call what this device cannot expose,
    while a missing Skill, MCP server or Plugin is a gap the running expert
    closes itself through APM.
    """

    if generation is None:
        return [], False
    tool_ids = {
        name
        for name, contribution in getattr(generation, "tools", {}).items()
        if not is_mcp_tool(contribution)
    }
    tools_missing = spec.tools != ("*",) and any(
        item not in tool_ids for item in spec.tools
    )
    missing = [
        f"{kind}:{item}"
        for kind, selected, available in (
            ("skill", spec.skills, set(getattr(generation, "skills", {}))),
            (
                "mcp",
                resolve_mcp_selectors(spec.mcps, getattr(generation, "mcps", {})),
                set(getattr(generation, "mcps", {})),
            ),
            ("plugin", spec.plugins, set(getattr(generation, "plugins", {}))),
        )
        if selected != ("*",)
        for item in selected
        if item not in available
    ]
    return missing, tools_missing


async def _providers(runtime) -> dict[str, object]:
    providers: dict[str, object] = {}
    for item in await runtime.model_configs.list_providers():
        # Provider records are stored under ids such as
        # ``custom-provider-chat`` while AgentModel uses the public provider
        # id ``custom:chat``. Keep both keys so availability projection and
        # model resolution agree on the same credential record.
        providers[item.id] = item
        provider_id = str(item.id)
        if provider_id.startswith("custom-provider-"):
            providers.setdefault("custom:" + provider_id.removeprefix("custom-provider-"), item)
    return providers


async def _validation_generation(runtime) -> object | None:
    if getattr(runtime, "role", "worker") != "control":
        return runtime.plugin_runtime.current_generation

    snapshot = control_catalog_snapshot(runtime)

    capabilities = snapshot["capabilities"]

    def indexed(kind: str) -> dict[str, object]:
        return {
            str(item["id"]): object()
            for item in capabilities[kind]
        }

    return SimpleNamespace(
        tools=indexed("tool"),
        skills=indexed("skill"),
        plugins={str(item): object() for item in snapshot["plugins"]},
        mcps={str(item): object() for item in snapshot["mcps"]},
    )


def _with_retained_agent_references(generation: object, current: AgentSpec) -> object:
    """Permit an edit to preserve selectors that were valid before removal."""

    return SimpleNamespace(**{
        field: {
            **dict(getattr(generation, field, {})),
            **{selector: object() for selector in (getattr(current, field) or ())},
        }
        for field in ("tools", "skills", "mcps", "plugins")
    })


def _avatar_url(runtime, agent_id: str) -> str | None:
    return (
        f"/api/v1/agents/{agent_id}/avatar"
        if runtime.agent_packages.avatar_path(agent_id) is not None
        else None
    )


def _execution_view(runtime, spec) -> dict:
    pool = getattr(runtime, "worker_supervisor", None)
    worker = getattr(pool, "_supervisors", {}).get(spec.id)
    status = worker.status_snapshot() if worker is not None else {"state": "stopped"}
    # 执行模式（host / sandbox）由 securityd 根据该 Agent 声明的安全设置推导，
    # 不在 Agent 配置里声明；这里只报告 Worker 自己看到的实际模式。
    return {**status}


@asynccontextmanager
async def _agent_edit_lock(runtime, agent_id, *, check_busy=True):
    pool = getattr(runtime, "worker_supervisor", None)
    locker = getattr(pool, "agent_lock", None)
    if not callable(locker):
        yield
        return
    async with locker(agent_id):
        if check_busy and pool.agent_busy(agent_id):
            raise HTTPException(409, detail={
                "code": "agent_busy", "message": "专家正在执行任务，请等待完成或停止任务后再修改。",
            })
        yield


def _process_metadata(spec) -> dict[str, object]:
    """Unknown metadata remains process configuration unless explicitly display-only."""
    metadata = dict(spec.metadata)
    metadata.pop("avatar", None)
    metadata.pop("execution", None)
    return metadata


def _requires_worker_restart(runtime, current, updated) -> bool:
    return _process_metadata(current) != _process_metadata(updated)


async def _start_agent_worker(runtime, spec, *, restart=False):
    pool = getattr(runtime, "worker_supervisor", None)
    if pool is None:
        return
    # 不指定模式：securityd 根据该 Agent 的安全设置推导 host / sandbox。
    worker = pool.for_agent(spec.id, requested_mode="auto")
    if restart:
        await worker.stop(reason="Agent configuration changed", strict=True)
    identity = await worker.start()


@router.get("", response_model=AgentCollectionEnvelope)
async def list_agents(request: Request) -> AgentCollectionEnvelope:
    runtime = request.app.state.runtime
    providers = await _providers(runtime)
    generation = await _validation_generation(runtime)
    return AgentCollectionEnvelope(
        data=[
            agent_view(
                item,
                providers=providers,
                generation=generation,
                avatar_url=_avatar_url(runtime, item.id),
                system=runtime.agent_packages.is_system(item.id),
                execution=_execution_view(runtime, item),
            )
            for item in await runtime.agents.list()
        ]
    )


@router.get("/{agent_id}", response_model=AgentEnvelope)
async def get_agent(agent_id: str, request: Request) -> AgentEnvelope:
    runtime = request.app.state.runtime
    generation = await _validation_generation(runtime)
    return AgentEnvelope(
        data=agent_view(
            await runtime.agents.get(agent_id),
            providers=await _providers(runtime),
            generation=generation,
            avatar_url=_avatar_url(runtime, agent_id),
            system=runtime.agent_packages.is_system(agent_id),
            execution=_execution_view(runtime, await runtime.agents.get(agent_id)),
        )
    )


@router.post("/{agent_id}/restart", response_model=AgentEnvelope)
async def restart_agent(agent_id: str, request: Request) -> AgentEnvelope:
    runtime = request.app.state.runtime
    async with _agent_edit_lock(runtime, agent_id):
        spec = await runtime.agents.get(agent_id)
        if getattr(runtime, "worker_supervisor", None) is None:
            raise HTTPException(503, detail={"code": "worker_unavailable", "message": "当前 Runtime 不支持托管 Worker 重建。"})
        try:
            await _start_agent_worker(runtime, spec, restart=True)
        except Exception as exc:
            raise HTTPException(503, detail={"code": "worker_restart_failed", "message": "Worker 重建失败，请检查运行状态后重试；未确认策略生效。"}) from exc
        return await get_agent(agent_id, request)


@router.post("", status_code=201, response_model=AgentEnvelope)
async def create_agent(payload: AgentCreateRequest, request: Request) -> AgentEnvelope:
    runtime = request.app.state.runtime
    spec = AgentSpec(
        id=new_agent_id(),
        name=payload.name,
        name_zh=payload.name_zh,
        name_en=payload.name_en,
        description=payload.description,
        description_zh=payload.description_zh,
        soul=payload.soul,
        model=(
            AgentModel(payload.model.provider, payload.model.name)
            if payload.model is not None
            else None
        ),
        tools=payload.tools,
        skills=payload.skills,
        mcps=payload.mcps,
        plugins=payload.plugins,
        permissions=payload.permissions,
        metadata=payload.metadata,
    )
    async with _agent_edit_lock(runtime, spec.id):
        async with runtime.catalog_mutation_lock:
            stored = await runtime.agents.create(
                spec,
                generation=await _validation_generation(runtime),
                allow_model_repair=True,
            )
        try:
            registration = await runtime.register_logical_agent(stored)
            if registration is None and runtime._security_registration_enabled():
                runtime._security_pending[stored.id] = "securityd unavailable"
            pool = getattr(runtime, "worker_supervisor", None)
            if pool is not None:
                pool.activate_agent(stored.id, requested_mode="auto")
                # ControlRunManager starts the Worker on first use. Creating
                # a definition must not wait for process/sandbox initialization.
        except Exception as exc:
            pool = getattr(runtime, "worker_supervisor", None)
            if runtime.security_registration_required or pool is not None:
                cleanup_errors = []
                if pool is not None:
                    try:
                        await pool.remove_agent(stored.id)
                    except Exception as cleanup_exc:
                        cleanup_errors.append(str(cleanup_exc))
                try:
                    await runtime.unregister_logical_agent(stored.id)
                except Exception as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
                await runtime.agents.delete(stored.id)
                if pool is not None:
                    message = f"专家创建失败：{exc}"
                    if cleanup_errors:
                        message += "；清理未完成：" + "; ".join(cleanup_errors)
                    raise HTTPException(503, detail={"code": "agent_create_failed", "message": message}) from exc
                raise
            runtime._security_pending[stored.id] = "registration failed"
        return AgentEnvelope(
            data=agent_view(
                stored,
                execution=_execution_view(runtime, stored),
                providers=await _providers(runtime),
                generation=await _validation_generation(runtime),
            ),
        )


@router.patch("/{agent_id}", response_model=AgentEnvelope)
async def update_agent(
    agent_id: str,
    payload: AgentUpdateRequest,
    request: Request,
) -> AgentEnvelope:
    runtime = request.app.state.runtime
    async with _agent_edit_lock(runtime, agent_id, check_busy=False):
        async with runtime.catalog_mutation_lock:
            current = await runtime.agents.get(agent_id)
            values: dict[str, object] = {
                "id": current.id,
                "name": current.name,
                "name_zh": current.name_zh,
                "name_en": current.name_en,
                "description": current.description,
                "description_zh": current.description_zh,
                "soul": current.soul,
                "model": current.model,
                "tools": current.tools,
                "skills": current.skills,
                "mcps": current.mcps,
                "plugins": current.plugins,
                "permissions": current.permissions,
                "metadata": current.metadata,
            }
            if "name" in payload.model_fields_set and payload.name is None:
                raise ValueError("agent name must not be null")
            for field in payload.model_fields_set:
                value = getattr(payload, field)
                if field in {
                    "name_zh",
                    "name_en",
                    "description",
                    "description_zh",
                    "soul",
                } and value is None:
                    value = ""
                elif field == "model":
                    value = (
                        AgentModel(value.provider, value.name)
                        if value is not None
                        else None
                    )
                elif field in {"tools", "skills", "mcps", "plugins"} and value is None:
                    value = ()
                elif field == "permissions" and value is None:
                    value = {}
                elif field == "metadata" and value is None:
                    value = {}
                values[field] = value
            updated = AgentSpec(**values)
            restart = _requires_worker_restart(runtime, current, updated)
            register = restart or current.name != updated.name
            pool = getattr(runtime, "worker_supervisor", None)
            # A live run owns its original AgentSpec. Model/prompt/display edits
            # are safe, but changing its process configuration is not.
            if pool is not None and restart and pool.agent_busy(agent_id):
                raise HTTPException(409, detail={
                    "code": "agent_busy",
                    "message": "专家正在执行任务，请等待完成或停止任务后再修改权限、能力或运行模式。",
                })
            generation = await _validation_generation(runtime)
            stored = await runtime.agents.update(
                updated,
                # Keeping a selector that was valid before its package was
                # removed must stay possible, but only for this write: the
                # projection below still reports the gap so Control and the
                # expert can close it through APM.
                generation=_with_retained_agent_references(generation, current),
                allow_model_repair=True,
            )
            previous_worker = getattr(pool, "_supervisors", {}).get(agent_id)
            previous_mode = getattr(previous_worker, "requested_mode", None)
            previous_ready = bool(previous_worker and previous_worker.ready)
            worker_changed = False
            try:
                if register:
                    registration = await runtime.register_logical_agent(stored)
                    if registration is None and runtime._security_registration_enabled():
                        runtime._security_pending[stored.id] = "securityd unavailable"
                if pool is not None:
                    if previous_ready and restart:
                        worker_changed = True
                        await _start_agent_worker(runtime, stored, restart=True)
                    elif not previous_ready:
                        pool.for_agent(stored.id, requested_mode="auto")
            except Exception as exc:
                rollback_errors = []
                await runtime.agents.update(current, allow_model_repair=True)
                if register:
                    try:
                        await runtime.register_logical_agent(current)
                    except Exception as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                if pool is not None and worker_changed:
                    worker = pool.for_agent(agent_id)
                    try:
                        await worker.stop(reason="Agent update rollback", strict=True)
                        worker.requested_mode = previous_mode or "auto"
                        if previous_ready:
                            await worker.start()
                    except Exception as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
                message = f"专家配置未应用，已恢复原配置：{exc}"
                if rollback_errors:
                    message += "；恢复运行失败：" + "; ".join(rollback_errors)
                raise HTTPException(503, detail={"code": "agent_update_failed", "message": message}) from exc
    return AgentEnvelope(
        data=agent_view(
            stored,
            providers=await _providers(runtime),
            generation=generation,
            avatar_url=_avatar_url(runtime, agent_id),
            system=runtime.agent_packages.is_system(agent_id),
            execution=_execution_view(runtime, await runtime.agents.get(agent_id)),
        )
    )


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, request: Request) -> Response:
    runtime = request.app.state.runtime
    async with _agent_edit_lock(runtime, agent_id, check_busy=False):
        # Reject package defaults before stopping a Worker or revoking security identity.
        runtime.agent_packages.assert_deletable(agent_id)
        worker_pool = getattr(runtime, "worker_supervisor", None)
        remove_worker = getattr(worker_pool, "remove_agent", None)
        activate_worker = getattr(worker_pool, "activate_agent", None)
        if callable(remove_worker):
            await remove_worker(agent_id)
        security_enabled = runtime._security_registration_enabled()
        try:
            unregistered = await runtime.unregister_logical_agent(agent_id)
        except Exception:
            if runtime.security_registration_required:
                if callable(activate_worker):
                    activate_worker(agent_id)
                raise
            unregistered = None
        # Optional/development security clients return None when securityd cannot
        # be reached.  Do not remove the local declaration in that case: doing so
        # would leave a stale Agent in the Security console and make the two
        # registries diverge.  The caller can retry after securityd is restored.
        if security_enabled and unregistered is None:
            if callable(activate_worker):
                activate_worker(agent_id)
            raise HTTPException(
                status_code=503,
                detail="Agent security unregister is pending; securityd is unavailable",
            )
        deleted = await runtime.agents.delete(agent_id)
        if not deleted:
            if callable(activate_worker):
                activate_worker(agent_id)
            raise ResourceNotFoundError("agent", agent_id)
        return Response(status_code=204)


@router.post(
    "/{agent_id}/avatar",
    response_model=AgentEnvelope,
    openapi_extra={"requestBody": _AVATAR_BODY},
)
async def upload_agent_avatar(agent_id: str, request: Request) -> AgentEnvelope:
    runtime = request.app.state.runtime
    spec = await runtime.agents.get(agent_id)
    form = await request.form()
    file = form.get("file")
    if file is None or not hasattr(file, "read"):
        raise HTTPException(422, "file is required")
    upload = cast(UploadFile, file)
    media_type = upload.content_type or ""
    suffix = _AVATAR_SUFFIX.get(media_type)
    if suffix is None:
        raise HTTPException(415, "avatar must be a supported image")
    payload = await upload.read(_AVATAR_LIMIT + 1)
    if len(payload) > _AVATAR_LIMIT:
        raise HTTPException(413, "avatar exceeds 2 MiB")
    if not _valid_avatar_payload(media_type, payload):
        raise HTTPException(415, "avatar content does not match its image MIME type")
    runtime.agent_packages.write_avatar(spec.id, suffix, payload)
    return AgentEnvelope(
        data=agent_view(
            spec,
            providers=await _providers(runtime),
            avatar_url=f"/api/v1/agents/{spec.id}/avatar",
            system=runtime.agent_packages.is_system(spec.id),
        )
    )


@router.get("/{agent_id}/avatar")
async def get_agent_avatar(agent_id: str, request: Request) -> FileResponse:
    runtime = request.app.state.runtime
    spec = await runtime.agents.get(agent_id)
    target: Path | None = runtime.agent_packages.avatar_path(spec.id)
    if target is None:
        raise HTTPException(404, "Agent avatar was not found")
    return FileResponse(
        target,
        media_type=_AVATAR_MEDIA[target.suffix],
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{agent_id}/avatar", status_code=204)
async def delete_agent_avatar(agent_id: str, request: Request) -> Response:
    runtime = request.app.state.runtime
    spec = await runtime.agents.get(agent_id)
    if not runtime.agent_packages.delete_avatar(spec.id):
        raise HTTPException(404, "Agent avatar was not found")
    return Response(status_code=204)


def _valid_avatar_payload(media_type: str, payload: bytes) -> bool:
    from bianbu_agent.models.vision import sniff_image_media_type

    return sniff_image_media_type(payload) == media_type


__all__ = ["agent_view", "router"]
