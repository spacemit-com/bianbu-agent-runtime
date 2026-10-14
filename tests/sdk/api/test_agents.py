from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from bianbu_agent import AgentSpec, BianbuAgentRuntime, RuntimeConfig
from bianbu_agent.server.app import create_app
from bianbu_agent.api.agents import _availability
from bianbu_agent.agents import AgentModel
from bianbu_agent.models.providers import ProviderConfig
from bianbu_agent.provisioning import AGENT_ID_PATTERN
from tests.sdk.api.test_runs import _api, _graph_api


def test_agent_availability_accepts_record_id_provider_alias() -> None:
    spec = AgentSpec(
        id="default", name="Default",
        model=AgentModel(provider="custom:chat", name="qwen3:0.6b"),
    )
    provider = ProviderConfig(
        id="custom-provider-chat", name="CHAT", protocol="openai-compatible",
        base_url="http://127.0.0.1:11434/v1", api_key="ollama",
    )
    assert _availability(spec, {provider.id: provider}) == {
        "available": True, "unavailable_reason": "", "missing_powers": []
    }


@asynccontextmanager
async def _file_backed_api(root: Path):
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=root / ".bianbu-agent"),
        default_model=FakeMessagesListChatModel(
            responses=[AIMessage(content="unused")]
        ),
        agent_id="default",
    )
    runtime.agent_packages.create(AgentSpec(id="default", name="Default"))
    await runtime.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False)),
            base_url="http://test",
        ) as client:
            yield client, runtime
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_agents_ignore_a_placeholder_inventory_file(tmp_path: Path) -> None:
    """用户目录里的 0 字节 inventory.sqlite3 不能让专家列表变成 422。

    K3 上真实发生过：那个占位文件让 `/api/v1/agents` 返回
    `invalid capability Inventory`，专家页显示 runtime 不可用。
    """

    home = tmp_path / ".bianbu-agent"
    home.mkdir(parents=True)
    (home / "inventory.sqlite3").write_bytes(b"")
    async with _file_backed_api(tmp_path) as (client, _):
        listing = await client.get("/api/v1/agents")

    assert listing.status_code == 200, listing.text


@pytest.mark.asyncio
async def test_agents_retire_an_unloadable_user_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧版写下的 agent.yaml 会被退休，不再被“猜着跳过”。

    K3 上真实发生过：用户目录里那份带 `hooks: []` 的专家记录会盖住发行版自带
    的同名专家。读取路径现在把读不出来的记录退休到 staging 并告警，于是专家
    列表、详情和头像都回到发行版自带的那份声明上。
    """

    monkeypatch.setenv("BIANBU_SYSTEM_RESOURCE_ROOT", str(tmp_path / "system"))
    packaged = tmp_path / "system" / "agents" / "starter"
    packaged.mkdir(parents=True)
    (packaged / "agent.yaml").write_text(
        "schema_version: 1\nagent:\n  id: starter\n  version: 1.0.0\n"
        "  name: Starter\n",
        encoding="utf-8",
    )
    async with _file_backed_api(tmp_path) as (client, runtime):
        broken = runtime.home.agents_dir / "starter"
        broken.mkdir()
        (broken / "agent.yaml").write_text(
            "schema_version: 1\nagent:\n  id: starter\n  version: 1.0.0\n"
            "  name: Starter\n  hooks: []\n",
            encoding="utf-8",
        )
        listing = await client.get("/api/v1/agents")
        detail = await client.get("/api/v1/agents/starter")
        avatar = await client.get("/api/v1/agents/starter/avatar")

    assert listing.status_code == 200, listing.text
    listed = {item["id"]: item for item in listing.json()["data"]}
    assert "starter" in listed
    assert listed["starter"]["name"] == "Starter"
    assert listed["starter"]["origin"] == "system"
    assert detail.status_code == 200, detail.text
    assert avatar.status_code == 404
    assert not broken.exists()
    assert list(runtime.home.staging_dir.glob("retired-agent-starter-*"))


@pytest.mark.asyncio
async def test_agents_share_one_read_write_resource_namespace(tmp_path: Path) -> None:
    """Agent reads and writes share the single public versioned API."""

    async with _api(tmp_path) as (client, _):
        listing = await client.get("/api/v1/agents")
        detail = await client.get("/api/v1/agents/writer")

    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["data"]] == ["default", "writer"]
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == "writer"


@pytest.mark.asyncio
async def test_agent_create_assigns_platform_agent_id(tmp_path: Path) -> None:
    """Every HTTP-created Agent receives the platform-owned ID format."""

    async with _api(tmp_path) as (client, _):
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "generated-id-provider",
                    "base_url": "https://model.test/v1",
                    "api_key": "secret",
                },
            )
        ).status_code == 201
        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "Generated ID",
                "model": {
                    "provider": "generated-id-provider",
                    "name": "chat",
                },
            },
        )

    assert created.status_code == 201
    assert AGENT_ID_PATTERN.fullmatch(created.json()["data"]["id"])


@pytest.mark.asyncio
async def test_agents_create_patch_and_delete_without_erasing_omitted_fields(
    tmp_path: Path,
) -> None:
    """An Agent PATCH changes only explicitly supplied fields."""

    async with _graph_api(tmp_path, plugins=("*",)) as (client, _, _):
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "editor-provider",
                    "label": "Editor Provider",
                    "base_url": "https://model.test/v1",
                    "api_key": "secret",
                    "parameters": {"protocol": "openai-compatible"},
                },
            )
        ).status_code == 201
        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "Editor",
                "name_zh": "编辑专家",
                "description": "Original description",
                "soul": "Keep sources",
                "model": {"provider": "editor-provider", "name": "editor-model"},
                "permissions": {
                    "level": "L2",
                    "network": {"mode": "allowlist", "domains": []},
                },
                "metadata": {"avatar": "✍️", "team": "docs"},
            },
        )
        assert created.status_code == 201
        agent_id = created.json()["data"]["id"]

        patched = await client.patch(
            f"/api/v1/agents/{agent_id}", json={"description": "Revised description"}
        )
        assert patched.status_code == 200
        item = patched.json()["data"]
        assert item["name"] == "Editor"
        assert item["name_zh"] == "编辑专家"
        assert item["description"] == "Revised description"
        assert item["soul"] == "Keep sources"
        assert item["model"] == {
            "provider": "editor-provider",
            "name": "editor-model",
        }
        assert item["tools"] == []
        assert item["skills"] == []
        assert item["permissions"] == {
            "level": "L2",
            "network": {"mode": "allowlist", "domains": []},
        }
        assert item["metadata"] == {"avatar": "✍️", "team": "docs"}

        rejected_null_name = await client.patch(
            f"/api/v1/agents/{agent_id}", json={"name": None}
        )
        assert rejected_null_name.status_code == 422
        assert rejected_null_name.json()["error"]["code"] == "validation_error"
        unchanged = await client.get(f"/api/v1/agents/{agent_id}")
        assert unchanged.json()["data"]["name"] == "Editor"

        cleared_model = await client.patch(
            f"/api/v1/agents/{agent_id}", json={"model": None}
        )
        assert cleared_model.status_code == 200
        assert cleared_model.json()["data"]["unavailable_reason"] == "agent_model_required"
        unchanged_model = await client.get(f"/api/v1/agents/{agent_id}")
        assert unchanged_model.json()["data"]["model"] is None

        deleted = await client.delete(f"/api/v1/agents/{agent_id}")
        assert deleted.status_code == 204
        assert (await client.get(f"/api/v1/agents/{agent_id}")).status_code == 404


@pytest.mark.asyncio
async def test_delete_agent_closes_its_worker_before_removing_declaration(
    tmp_path: Path,
) -> None:
    async with _file_backed_api(tmp_path) as (client, runtime):
        await runtime.agents.create(AgentSpec(id="research", name="Research"))
        events: list[tuple[str, str]] = []

        class Pool:
            async def remove_agent(self, agent_id: str) -> bool:
                events.append(("worker", agent_id))
                return True

            async def close(self) -> None:
                return None

        runtime.worker_supervisor = Pool()
        original_delete = runtime.agents.delete

        async def delete(agent_id: str) -> bool:
            events.append(("declaration", agent_id))
            return await original_delete(agent_id)

        runtime.agents.delete = delete

        removed = await client.delete("/api/v1/agents/research")

    assert removed.status_code == 204
    assert events == [("worker", "research"), ("declaration", "research")]


@pytest.mark.asyncio
async def test_delete_agent_keeps_declaration_when_worker_cannot_stop(
    tmp_path: Path,
) -> None:
    async with _file_backed_api(tmp_path) as (client, runtime):
        await runtime.agents.create(AgentSpec(id="research", name="Research"))

        class Pool:
            async def remove_agent(self, agent_id: str) -> bool:
                assert agent_id == "research"
                raise RuntimeError("worker did not stop")

            async def close(self) -> None:
                return None

        runtime.worker_supervisor = Pool()
        with pytest.raises(RuntimeError, match="worker did not stop"):
            await client.delete("/api/v1/agents/research")

        assert await runtime.agents.get("research") is not None


@pytest.mark.asyncio
async def test_agent_avatar_is_stored_served_and_deleted_with_its_agent(
    tmp_path: Path,
) -> None:
    """An uploaded avatar belongs to exactly one Agent package."""

    png = b"\x89PNG\r\n\x1a\nagent-avatar"
    async with _file_backed_api(tmp_path) as (client, runtime):
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "avatar-provider",
                    "label": "Avatar Provider",
                    "base_url": "https://model.test/v1",
                    "api_key": "secret",
                    "parameters": {"protocol": "openai-compatible"},
                },
            )
        ).status_code == 201
        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "Portrait",
                "model": {"provider": "avatar-provider", "name": "chat"},
            },
        )
        assert created.status_code == 201
        agent_id = created.json()["data"]["id"]

        uploaded = await client.post(
            f"/api/v1/agents/{agent_id}/avatar",
            files={"file": ("payload.html", png, "image/png")},
        )
        package_root = runtime.agent_packages.home.agents_dir / agent_id
        fetched = await client.get(f"/api/v1/agents/{agent_id}/avatar")
        detail = await client.get(f"/api/v1/agents/{agent_id}")
        cleared = await client.delete(f"/api/v1/agents/{agent_id}/avatar")
        after_clear = await client.get(f"/api/v1/agents/{agent_id}/avatar")
        removed = await client.delete(f"/api/v1/agents/{agent_id}")

    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["data"]["avatar"] == f"/api/v1/agents/{agent_id}/avatar"
    assert detail.json()["data"]["avatar"] == f"/api/v1/agents/{agent_id}/avatar"
    assert fetched.content == png
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.headers["x-content-type-options"] == "nosniff"
    assert cleared.status_code == 204
    assert after_clear.status_code == 404
    assert removed.status_code == 204
    assert not package_root.exists()


@pytest.mark.asyncio
async def test_agent_avatar_rejects_spoofed_oversized_and_unknown_uploads(
    tmp_path: Path,
) -> None:
    """The Agent child resource validates ownership, MIME, signature, and size."""

    async with _file_backed_api(tmp_path) as (client, _):
        missing = await client.post(
            "/api/v1/agents/missing/avatar",
            files={"file": ("avatar.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        bad_type = await client.post(
            "/api/v1/agents/default/avatar",
            files={"file": ("avatar.txt", b"bad", "text/plain")},
        )
        spoofed = await client.post(
            "/api/v1/agents/default/avatar",
            files={"file": ("avatar.png", b"<html>", "image/png")},
        )
        oversized = await client.post(
            "/api/v1/agents/default/avatar",
            files={
                "file": (
                    "avatar.png",
                    b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024 * 1024),
                    "image/png",
                )
            },
        )

    assert missing.status_code == 404
    assert bad_type.status_code == spoofed.status_code == 415
    assert oversized.status_code == 413


@pytest.mark.asyncio
async def test_removed_admin_model_file_and_avatar_routes_are_absent(
    tmp_path: Path,
) -> None:
    """Removed route families must not survive as aliases."""

    async with _api(tmp_path) as (client, _):
        responses = [
            await client.get("/api/admin/v1/agents"),
            await client.get("/api/v1/models"),
            await client.get("/api/v1/capability-bindings"),
            await client.get("/api/v1/files", params={"path": "anything"}),
            await client.get("/api/v1/avatars"),
        ]

    assert [response.status_code for response in responses] == [404, 404, 404, 404, 404]


@pytest.mark.asyncio
async def test_any_agent_can_be_deleted_and_legacy_agent_paths_are_absent(
    tmp_path: Path,
) -> None:
    """No Agent is globally protected and flat Agent routes do not survive."""

    async with _api(tmp_path) as (client, _):
        deleted = await client.delete("/api/v1/agents/default")
        legacy_list = await client.get("/api/agents")
        legacy_create = await client.post(
            "/api/agents", json={"id": "legacy", "name": "Legacy"}
        )

    assert deleted.status_code == 204
    assert [legacy_list.status_code, legacy_create.status_code] == [404, 404]


@pytest.mark.asyncio
async def test_agent_structured_model_and_mcps_round_trip_and_validate_provider(
    tmp_path: Path,
) -> None:
    """Dropping provider/name or MCP fields must break Agent-owned execution config."""

    async with _api(tmp_path) as (client, _):
        provider = await client.post(
            "/api/v1/providers",
            json={
                "id": "openrouter",
                "label": "OpenRouter",
                "base_url": "https://openrouter.test/v1",
                "api_key": "provider-secret",
                "parameters": {"protocol": "openai-compatible"},
            },
        )
        assert provider.status_code == 201

        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "Researcher",
                "model": {"provider": "openrouter", "name": "qwen/qwen3"},
                "mcps": [],
            },
        )
        missing_provider = await client.post(
            "/api/v1/agents",
            json={
                "name": "Orphan",
                "model": {"provider": "missing", "name": "model"},
                "mcps": [],
            },
        )

    assert created.status_code == 201
    assert created.json()["data"]["model"] == {
        "provider": "openrouter",
        "name": "qwen/qwen3",
    }
    assert created.json()["data"]["mcps"] == []
    assert missing_provider.status_code == 422
    assert missing_provider.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_agent_create_and_patch_allow_model_to_be_configured_later(tmp_path: Path) -> None:
    """Model-less experts persist with an actionable unavailable reason."""

    async with _api(tmp_path) as (client, _):
        missing = await client.post(
            "/api/v1/agents",
            json={"name": "Missing Model"},
        )
        null = await client.post(
            "/api/v1/agents",
            json={"name": "Null Model", "model": None},
        )
        cleared = await client.patch(
            "/api/v1/agents/writer",
            json={"model": None},
        )

    for response in (missing, null, cleared):
        assert response.status_code in {200, 201}
        assert response.json()["data"]["model"] is None
        assert response.json()["data"]["available"] is False
        assert response.json()["data"]["unavailable_reason"] == "agent_model_required"


@pytest.mark.asyncio
async def test_legacy_string_agent_projects_null_and_diagnostic_state(tmp_path: Path) -> None:
    """Legacy model IDs may be diagnosed but can never leak as the current schema."""

    async with _api(tmp_path) as (client, runtime):
        await runtime._agent_store.update(
            __import__("bianbu_agent").AgentSpec(
                id="writer",
                name="Writer",
                model="legacy-model-id",
                metadata={"legacy_model": "legacy-model-id"},
            )
        )
        response = await client.get("/api/v1/agents/writer")

    assert response.status_code == 200
    assert response.json()["data"]["model"] is None
    assert response.json()["data"]["available"] is False
    assert response.json()["data"]["unavailable_reason"] == "agent_model_required"


@pytest.mark.asyncio
async def test_agent_view_reports_preexisting_missing_capability(tmp_path: Path) -> None:
    """An out-of-band package deletion stays visible without blocking the Agent.

    The expert still has to run, because provisioning the missing Power is what
    the Agent does for itself through APM.
    """

    async with _api(tmp_path) as (client, runtime):
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "bound",
                    "base_url": "https://model.test/v1",
                    "api_key": "secret",
                },
            )
        ).status_code == 201
        await runtime._agent_store.update(
            __import__("bianbu_agent").AgentSpec(
                id="writer",
                name="Writer",
                model=__import__("bianbu_agent").AgentModel("bound", "chat"),
                skills=("deleted-skill",),
            )
        )

        response = await client.get("/api/v1/agents/writer")

    assert response.status_code == 200
    assert response.json()["data"]["available"] is True
    assert response.json()["data"]["unavailable_reason"] == ""
    assert response.json()["data"]["missing_powers"] == ["skill:deleted-skill"]


def test_agent_openapi_uses_typed_item_and_collection_envelopes() -> None:
    """Agent schemas must be explicit enough for generated clients to trust."""

    from tests.sdk.api.test_contracts import _application

    document = _application().openapi()
    assert document["paths"]["/api/v1/agents"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AgentCollectionEnvelope"
    }
    assert document["paths"]["/api/v1/agents/{agent_id}"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AgentEnvelope"
    }


@pytest.mark.asyncio
async def test_provider_delete_reports_agent_references(tmp_path: Path) -> None:
    """Deleting an Agent-owned provider must not orphan runtime model selection."""

    async with _api(tmp_path) as (client, _):
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "bound",
                    "label": "Bound",
                    "base_url": "https://model.test/v1",
                    "api_key": "secret",
                    "parameters": {"protocol": "openai-compatible"},
                },
            )
        ).status_code == 201
        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "Bound Agent",
                "model": {"provider": "bound", "name": "chat-model"},
            },
        )
        assert created.status_code == 201
        agent_id = created.json()["data"]["id"]

        conflict = await client.delete("/api/v1/providers/bound")

    assert conflict.status_code == 409
    assert conflict.json() == {
        "error": {
            "code": "provider_in_use",
            # The message names the Expert, because ``agent_id`` is a content
            # hash and a refusal that only quotes it is unactionable.
            "message": (
                f"服务商 bound 正在被专家使用：Bound Agent（{agent_id}）。"
                "请先在这些专家里换用其它服务商或删除这些专家，再删除该服务商。"
            ),
            "details": {
                "provider_id": "bound",
                "agent_ids": [agent_id],
                "agents": [
                    {
                        "id": agent_id,
                        "name": "Bound Agent",
                        "name_zh": "",
                        "name_en": "",
                    }
                ],
            },
        }
    }


@pytest.mark.asyncio
async def test_deleting_a_missing_expert_or_provider_names_the_resource(
    tmp_path: Path,
) -> None:
    """404 from a mutation must name the resource instead of echoing a key.

    ``KeyError`` renders as the bare key with quotes stripped, so clients that
    show the message displayed ``com.bianbu....`` or
    ``custom-provider-<name>`` -- neither the ID the user clicked nor any hint
    about what happened.
    """

    async with _api(tmp_path) as (client, _):
        missing_agent = await client.delete("/api/v1/agents/com.bianbu.gone")
        missing_provider = await client.delete("/api/v1/providers/custom:gone")

    assert missing_agent.status_code == 404
    assert missing_agent.json() == {
        "error": {
            "code": "not_found",
            "message": "找不到专家 com.bianbu.gone，它可能已被删除。",
            "details": {"kind": "agent", "id": "com.bianbu.gone"},
        }
    }
    assert missing_provider.status_code == 404
    assert missing_provider.json() == {
        "error": {
            "code": "not_found",
            "message": "找不到服务商 custom:gone，它可能已被删除。",
            "details": {"kind": "provider", "id": "custom:gone"},
        }
    }


@pytest.mark.asyncio
async def test_provider_model_discovery_uses_secret_without_exposing_it(
    tmp_path: Path,
) -> None:
    """Provider discovery must call the upstream subresource with stored auth."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://model.test/v1/models"
        assert request.headers["Authorization"] == "Bearer provider-secret"
        return httpx.Response(
            200,
            json={"data": [{"id": "model-b"}, {"id": "model-a"}]},
        )

    async with _api(tmp_path) as (client, runtime):
        runtime.provider_model_transport = httpx.MockTransport(respond)
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "discovery",
                    "label": "Discovery",
                    "base_url": "https://model.test/v1",
                    "api_key": "provider-secret",
                    "parameters": {"protocol": "openai-compatible"},
                },
            )
        ).status_code == 201

        response = await client.get(
            "/api/v1/providers/discovery/models"
        )

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {"id": "model-a", "name": "model-a"},
            {"id": "model-b", "name": "model-b"},
        ]
    }
    assert "provider-secret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status,code", [
    (httpx.ConnectTimeout("timed out"), 504, "provider_timeout"),
    (httpx.ConnectError("Connection refused"), 502, "provider_connection_failed"),
])
async def test_provider_model_discovery_exposes_timeout_and_connection_reasons(
    tmp_path: Path, failure: Exception, status: int, code: str,
) -> None:
    """控制台拉模型列表失败时要说清是超时还是连不上。

    以前一律 422 validation_error + 英文 "provider model discovery timed out"，
    控制台又折叠成「无法获取模型列表，请手动输入模型名称」—— 用户分不清
    是网络、地址写错，还是 Key 过期。
    """

    def respond(request: httpx.Request) -> httpx.Response:
        raise failure

    async with _api(tmp_path) as (client, runtime):
        runtime.provider_model_transport = httpx.MockTransport(respond)
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "deadprovider",
                    "label": "Dead",
                    "base_url": "https://model.test/v1",
                    "api_key": "provider-secret",
                    "parameters": {"protocol": "openai-compatible"},
                },
            )
        ).status_code == 201

        response = await client.get("/api/v1/providers/deadprovider/models")

    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["details"]["provider_host"] == "model.test"
    assert "model.test" in error["message"]
    assert "provider-secret" not in response.text


@pytest.mark.asyncio
async def test_provider_model_discovery_reports_rejected_credentials(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    async with _api(tmp_path) as (client, runtime):
        runtime.provider_model_transport = httpx.MockTransport(respond)
        assert (
            await client.post(
                "/api/v1/providers",
                json={
                    "id": "authfail",
                    "label": "Auth",
                    "base_url": "https://model.test/v1",
                    "api_key": "provider-secret",
                    "parameters": {"protocol": "openai-compatible"},
                },
            )
        ).status_code == 201

        response = await client.get("/api/v1/providers/authfail/models")

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "provider_auth_failed"
    assert error["details"]["http_status"] == 401
    assert "API Key" in error["message"]


@pytest.mark.asyncio
async def test_agents_reject_removed_agent_level_hooks(tmp_path: Path) -> None:
    """Hooks belong to Plugins, so the Agent API no longer accepts them."""

    async with _graph_api(tmp_path, plugins=("*",)) as (client, _, _):
        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "Editor",
                "model": {"provider": "editor-provider", "name": "editor-model"},
                "hooks": ["vendor.bundle/audit.policy"],
            },
        )
        assert created.status_code == 422, created.text
        assert created.json()["error"]["code"] == "validation_error"
