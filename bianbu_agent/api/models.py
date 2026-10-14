"""Provider HTTP resources and internal model configuration helpers."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from bianbu_agent._internal.json_records import validate_record_id
from bianbu_agent.agents import AgentModel
from bianbu_agent.api.contracts import (
    ProviderCreateRequest,
    ProviderUpdateRequest,
    SuccessEnvelope,
)
from bianbu_agent.api.errors import success
from bianbu_agent.errors import AgentReference, ProviderInUseError, ResourceNotFoundError
from bianbu_agent.models import (
    ModelConfig,
    YAMLModelStore,
    model_base_url_view,
    model_config_view,
)
from bianbu_agent.models.providers import normalize_provider_configuration
from bianbu_agent.security.security_errors import classify_run_exception


router = APIRouter()


def _discovery_error(exc: BaseException, provider: object, *, status_code: int) -> HTTPException:
    """Provider 拉不到模型列表时，给出前端能直接显示的具体原因。

    以前一律 422 ``validation_error`` + 英文 "provider model discovery timed out"，
    而控制台又把任何失败折叠成「无法获取模型列表，请手动输入模型名称」——
    用户看不出是超时、是认证失败，还是域名写错了。这里复用 Run 侧同一套
    分类（provider_timeout / provider_connection_failed），错误码与文案保持一致。
    """
    payload = classify_run_exception(
        exc,
        provider_base_url=getattr(provider, "base_url", ""),
        network_policy=None,
    )
    details = dict(payload.get("details") or {})
    details["provider_id"] = str(getattr(provider, "id", ""))
    return HTTPException(
        status_code=status_code,
        detail={
            "code": payload["code"],
            "message": payload["message"],
            "details": details,
        },
    )


_PROVIDER_KINDS = frozenset(
    {"provider_credential", "custom_provider", "project_capability_provider"}
)
def provider_view(config: ModelConfig, *, origin: str = "user") -> dict[str, object]:
    """Return a credential-safe Provider representation.

    ``origin`` mirrors the Agent view: ``system`` means the package ships this
    Provider, so deleting it only writes a removal marker and a reinstall (or a
    re-created record with the same ID) restores it.  Clients need the
    distinction because ``record_kind`` is ``custom_provider`` for packaged and
    user-created Providers alike -- which is how every hand-made Provider got
    labelled "factory" in the Bibit UI.
    """

    parameters = dict(config.parameters)
    public = model_config_view(config)
    normalized_base_url, api_key = normalize_provider_configuration(
        config.base_url,
        parameters.get("api_key"),
    )
    base_url = model_base_url_view(normalized_base_url)
    return {
        "id": _provider_public_id(config),
        "label": str(parameters.get("name") or _provider_public_id(config)),
        "base_url": base_url,
        "configured": bool(normalized_base_url and api_key),
        "api_key_masked": _mask_secret(api_key),
        "origin": origin,
        "parameters": public["parameters"],
    }


async def list_providers(store: YAMLModelStore) -> list[dict[str, object]]:
    packaged = await store.packaged_record_ids()
    return [
        provider_view(item, origin="system" if item.id in packaged else "user")
        for item in await store.list()
        if _is_provider(item)
    ]


async def get_provider(store: YAMLModelStore, provider_id: str) -> ModelConfig:
    item = await store.get(_provider_record_id(provider_id))
    if item is None or not _is_provider(item):
        raise KeyError(provider_id)
    return item


async def create_provider_resource(
    store: YAMLModelStore,
    payload: ProviderCreateRequest,
) -> ModelConfig:
    provider_id, record_id = _provider_identity(payload.id)
    parameters = _provider_parameters(
        provider_id,
        label=payload.label,
        api_key=payload.api_key,
        supplied=payload.parameters,
    )
    return await store.create(
        ModelConfig(
            id=record_id,
            provider=provider_id,
            model="credential",
            base_url=payload.base_url,
            parameters=parameters,
        )
    )


async def update_provider_resource(
    store: YAMLModelStore,
    provider_id: str,
    payload: ProviderUpdateRequest,
) -> ModelConfig:
    fields = payload.model_fields_set
    try:
        return await store.patch_provider_resource(
            _provider_record_id(provider_id),
            fields=frozenset(fields),
            label=payload.label,
            base_url=payload.base_url,
            api_key=payload.api_key,
            parameters=payload.parameters,
        )
    except KeyError as exc:
        raise _provider_not_found(provider_id) from exc


async def delete_provider_resource(store: YAMLModelStore, provider_id: str) -> None:
    try:
        deleted = await store.delete_provider_resource(_provider_record_id(provider_id))
    except KeyError as exc:
        raise _provider_not_found(provider_id) from exc
    if not deleted:
        raise _provider_not_found(provider_id)


def _provider_not_found(provider_id: str) -> ResourceNotFoundError:
    """Report a missing Provider by the ID the caller sent.

    The store works in record IDs (``custom-provider-<name>``); letting its
    ``KeyError`` escape rendered the bare record ID with quotes stripped, so a
    client showed ``custom-provider-gone`` for the ``custom:gone`` the user
    clicked.
    """

    return ResourceNotFoundError("provider", provider_id)


@router.get("/providers", response_model=SuccessEnvelope)
async def provider_collection(request: Request) -> JSONResponse:
    return success(await list_providers(request.app.state.runtime.model_configs))


@router.post("/providers", status_code=201, response_model=SuccessEnvelope)
async def create_provider(payload: ProviderCreateRequest, request: Request) -> JSONResponse:
    stored = await create_provider_resource(request.app.state.runtime.model_configs, payload)
    return success(provider_view(stored, origin="user"), status_code=201)


@router.get("/providers/{provider_id}", response_model=SuccessEnvelope)
async def provider_item(provider_id: str, request: Request) -> JSONResponse:
    store = request.app.state.runtime.model_configs
    stored = await get_provider(store, provider_id)
    return success(provider_view(stored, origin=await store.record_origin(stored.id)))


@router.patch("/providers/{provider_id}", response_model=SuccessEnvelope)
async def update_provider(
    provider_id: str,
    payload: ProviderUpdateRequest,
    request: Request,
) -> JSONResponse:
    store = request.app.state.runtime.model_configs
    stored = await update_provider_resource(store, provider_id, payload)
    return success(provider_view(stored, origin=await store.record_origin(stored.id)))


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(provider_id: str, request: Request) -> Response:
    runtime = request.app.state.runtime
    async with runtime.catalog_mutation_lock:
        references = tuple(
            sorted(
                (
                    AgentReference(
                        id=agent.id,
                        name=agent.name,
                        name_zh=agent.name_zh,
                        name_en=agent.name_en,
                    )
                    for agent in await runtime.agents.list()
                    if isinstance(agent.model, AgentModel)
                    and agent.model.provider == provider_id
                ),
                key=lambda reference: reference.id,
            )
        )
        if references:
            raise ProviderInUseError(provider_id, references)
        await delete_provider_resource(runtime.model_configs, provider_id)
    return Response(status_code=204)


@router.get("/providers/{provider_id}/models", response_model=SuccessEnvelope)
async def discover_provider_models(provider_id: str, request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    provider = await runtime.model_configs.get_provider(provider_id)
    if provider is None:
        raise KeyError(provider_id)
    if provider.protocol != "openai-compatible":
        raise ValueError(
            f"provider protocol does not support discovery: {provider.protocol}"
        )
    if not provider.configured:
        raise ValueError("provider is incomplete")
    transport = getattr(runtime, "provider_model_transport", None)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
            response = await client.get(
                provider.base_url.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {provider.api_key}"},
            )
            response.raise_for_status()
            document = response.json()
            from bianbu_agent.models.context_profiles import discovered_profiles, is_llama_server
            data = document.get("data") if isinstance(document, Mapping) else None
            props = None
            # llama.cpp exposes its training capacity in /models. Query its
            # serving allocation only for recognisable llama responses; other
            # providers incur no second request.
            if isinstance(data, list) and is_llama_server(data):
                try:
                    props_response = await client.get(
                        provider.base_url.rstrip("/").removesuffix("/v1") + "/props",
                        headers={"Authorization": f"Bearer {provider.api_key}"},
                        timeout=1.0,
                    )
                    props_response.raise_for_status()
                    props = props_response.json()
                except (httpx.HTTPError, ValueError):
                    pass
    except httpx.TimeoutException as exc:
        # 504：请求确实超时了，而不是用户填错了什么。
        raise _discovery_error(exc, provider, status_code=504) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "provider_auth_failed",
                    "message": (
                        "无法获取模型列表：Provider 拒绝了凭据。"
                        "下一步：检查 API Key 是否正确、是否已过期。"
                    ),
                    "details": {
                        "provider_id": str(provider.id),
                        "http_status": status,
                    },
                },
            ) from exc
        raise HTTPException(
            status_code=502,
            detail={
                "code": "provider_discovery_failed",
                "message": f"无法获取模型列表：Provider 返回 HTTP {status}。",
                "details": {
                    "provider_id": str(provider.id),
                    "http_status": status,
                },
            },
        ) from exc
    except httpx.HTTPError as exc:
        raise _discovery_error(exc, provider, status_code=502) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "provider_discovery_failed",
                "message": "无法获取模型列表：Provider 返回的内容无法解析。",
                "details": {"provider_id": str(provider.id)},
            },
        ) from exc
    data = document.get("data") if isinstance(document, Mapping) else None
    if not isinstance(data, list):
        # 走到了这里说明 HTTP 是通的、只是返回体不是 OpenAI /models 的形状
        # （例如把域名写成了网关首页）。原因要具体，用户才知道去改什么。
        raise HTTPException(
            status_code=502,
            detail={
                "code": "provider_discovery_failed",
                "message": (
                    "无法获取模型列表：Provider 返回的不是兼容 OpenAI 的 /models 响应。"
                    "下一步：确认 Base URL 指向 Provider 的 API 根路径（通常以 /v1 结尾）。"
                ),
                "details": {"provider_id": str(provider.id)},
            },
        )
    await runtime.model_configs.cache_provider_model_profiles(
        provider_id, discovered_profiles(data, props), base_url=provider.base_url
    )
    names = sorted(
        {
            str(item.get("id") or "").strip()
            for item in data
            if isinstance(item, Mapping) and str(item.get("id") or "").strip()
        },
        key=str.casefold,
    )
    return success([{"id": name, "name": name} for name in names])


def _provider_identity(provider_id: str) -> tuple[str, str]:
    selected = str(provider_id).strip()
    if selected.startswith("custom:"):
        custom_id = validate_record_id(selected.split(":", 1)[1])
        return f"custom:{custom_id}", f"custom-provider-{custom_id}"
    selected = validate_record_id(selected)
    return selected, f"provider-{selected}"


def _provider_record_id(provider_id: str) -> str:
    return _provider_identity(provider_id)[1]


def _provider_public_id(config: ModelConfig) -> str:
    return config.provider


def _provider_parameters(
    provider_id: str,
    *,
    label: str,
    api_key: str,
    supplied: Mapping[str, object],
) -> dict[str, object]:
    parameters = dict(supplied)
    parameters.update(
        {
            "record_kind": (
                "custom_provider" if provider_id.startswith("custom:") else "provider_credential"
            ),
            "name": str(label or provider_id),
            "api_key": str(api_key),
        }
    )
    if provider_id.startswith("custom:"):
        parameters["custom_id"] = provider_id.split(":", 1)[1]
    return parameters


def _is_provider(config: ModelConfig) -> bool:
    return config.model == "credential" and config.parameters.get("record_kind") in _PROVIDER_KINDS


def _mask_secret(value: object) -> str:
    secret = str(value or "")
    if not secret:
        return ""
    if len(secret) <= 6:
        return "*" * len(secret)
    return f"{secret[:3]}{'*' * max(3, len(secret) - 7)}{secret[-4:]}"


__all__ = [
    "create_provider_resource",
    "delete_provider_resource",
    "get_provider",
    "list_providers",
    "provider_view",
    "router",
    "update_provider_resource",
]
