from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException

from bianbu_agent.agents import AgentManifest, AgentModel, AgentSpec
from bianbu_agent.api import packages as packages_api
from bianbu_agent.mcp.client import MCPClient, MCPRemoteTool
from tests.sdk.api.test_runs import _api


def _skill(root: Path, name: str = "review") -> Path:
    source = root / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Review carefully\nversion: 1.0.0\n---\n\nReview.\n",
        encoding="utf-8",
    )
    return source


def _agent(root: Path) -> Path:
    source = root / "writer"
    source.mkdir(parents=True)
    manifest = AgentManifest(
        schema_version=1,
        version="1.0.0",
        spec=AgentSpec(
            id="writer",
            name="Writer",
            model=AgentModel("fixture", "fixture-model"),
            mcps=("mcp.weather",),
            metadata={"avatar": "pen"},
        ),
    )
    (source / "agent.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
    return source


def _plugin(root: Path, *, version: str = "1.0.0") -> Path:
    source = root / "vendor.bundle"
    _skill(source / "skills", "review")
    (source / "plugin.yaml").write_text(
        """\
schema_version: 1
name: vendor.bundle
version: {version}
description: Package fixture bundle.
""".format(version=version),
        encoding="utf-8",
    )
    return source


@pytest.mark.asyncio
async def test_typed_package_dispatch_installs_inspects_verifies_and_deletes_each_kind(
    tmp_path: Path,
) -> None:
    """A missing kind branch or manager call must break the common resource contract."""

    sources = {
        "skill": _skill(tmp_path / "sources" / "skills"),
        "agent": _agent(tmp_path / "sources" / "agents"),
    }
    async with _api(tmp_path / "runtime") as (client, _):
        for kind, source in sources.items():
            installed = await client.post(
                f"/api/v1/packages/{kind}",
                json={"source": str(source)},
            )
            assert installed.status_code == 201, installed.text
            view = installed.json()["data"]
            package_id = str(view["id"])
            assert view["kind"] == kind
            assert view["version"]
            assert len(view["content_digest"]) == 64
            if kind == "agent":
                assert view["agent"]["model"] == {
                    "provider": "fixture",
                    "name": "fixture-model",
                }
                assert view["agent"]["mcps"] == ["mcp.weather"]

            listing = await client.get(f"/api/v1/packages/{kind}")
            assert [item["id"] for item in listing.json()["data"]] == [package_id]
            detail = await client.get(
                f"/api/v1/packages/{kind}/{package_id}"
            )
            assert detail.json()["data"] == view
            verified = await client.post(
                f"/api/v1/packages/{kind}/{package_id}/verify"
            )
            assert verified.json() == {"data": {"valid": True}}

            removed = await client.delete(
                f"/api/v1/packages/{kind}/{package_id}"
            )
            assert removed.status_code == 204
            assert (
                await client.get(f"/api/v1/packages/{kind}/{package_id}")
            ).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "source_factory", "catalog_name", "capability_id"),
    (
        ("skill", _skill, "skills", "review"),
    ),
)
async def test_executable_package_mutations_publish_worker_generations(
    tmp_path: Path,
    kind: str,
    source_factory,
    catalog_name: str,
    capability_id: str,
) -> None:
    """Standalone executable packages become active and inactive immediately."""

    source = source_factory(tmp_path / "sources")
    async with _api(tmp_path / "runtime") as (client, runtime):
        initial = runtime.plugin_runtime.current_generation.id
        installed = await client.post(
            f"/api/v1/packages/{kind}",
            json={"source": str(source)},
        )
        active = runtime.plugin_runtime.current_generation
        removed = await client.delete(
            f"/api/v1/packages/{kind}/{capability_id}"
        )
        after_delete = runtime.plugin_runtime.current_generation

    assert installed.status_code == 201
    assert active.id != initial
    assert capability_id in getattr(active, catalog_name)
    assert removed.status_code == 204
    assert after_delete.id != active.id
    assert capability_id not in getattr(after_delete, catalog_name)


@pytest.mark.asyncio
async def test_package_activation_failure_rolls_back_disk_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package is not reported installed when no generation can publish it."""

    source = _skill(tmp_path / "sources")
    async with _api(tmp_path / "runtime") as (client, runtime):
        async def reject_candidate():
            raise RuntimeError("candidate rejected")

        monkeypatch.setattr(
            runtime.plugin_runtime,
            "reload_installed",
            reject_candidate,
        )
        response = await client.post(
            "/api/v1/packages/skill",
            json={"source": str(source)},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "package_activation_failed"
    assert runtime.skills.inspect("review") is None


@pytest.mark.asyncio
async def test_package_rollback_failure_preserves_worker_unavailable_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secondary disk failure must not mask the primary Worker 503."""

    source = _skill(tmp_path / "sources")
    async with _api(tmp_path / "runtime") as (client, runtime):
        async def worker_unavailable(_runtime):
            raise HTTPException(503, {"code": "worker_unavailable"})

        def rollback_failure(_package_id: str):
            raise OSError("synthetic rollback failure")

        monkeypatch.setattr(packages_api, "_reload_active_worker", worker_unavailable)
        monkeypatch.setattr(runtime.skills, "uninstall", rollback_failure)
        response = await client.post(
            "/api/v1/packages/skill",
            json={"source": str(source)},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "worker_unavailable"


@pytest.mark.asyncio
async def test_packages_reject_invalid_kinds_unknown_resources_and_action_bodies(
    tmp_path: Path,
) -> None:
    """Generated kind routes and action discriminators must not reappear."""

    async with _api(tmp_path) as (client, _):
        invalid_kind = await client.get("/api/v1/packages/widget")
        unknown = await client.get("/api/v1/packages/skill/missing")
        action = await client.post(
            "/api/v1/packages/skill",
            json={"source": str(tmp_path), "action": "install"},
        )

    assert invalid_kind.status_code == 422
    assert unknown.status_code == 404
    assert action.status_code == 422


@pytest.mark.asyncio
async def test_package_and_plugin_installs_reject_source_root_symlinks(
    tmp_path: Path,
) -> None:
    """Resolving a source before manager validation must not hide a symlink root."""

    sources = {
        "skill": _skill(tmp_path / "sources" / "skills"),
        "agent": _agent(tmp_path / "sources" / "agents"),
    }
    links: dict[str, Path] = {}
    for kind, source in sources.items():
        link = tmp_path / "links" / kind / source.name
        link.parent.mkdir(parents=True)
        link.symlink_to(source, target_is_directory=True)
        links[kind] = link
    plugin_source = _plugin(tmp_path / "plugin-source")
    plugin_link = tmp_path / "links" / "plugin" / plugin_source.name
    plugin_link.parent.mkdir(parents=True)
    plugin_link.symlink_to(plugin_source, target_is_directory=True)

    async with _api(tmp_path / "runtime") as (client, _):
        responses = {
            kind: await client.post(
                f"/api/v1/packages/{kind}", json={"source": str(link)}
            )
            for kind, link in links.items()
        }
        responses["plugin"] = await client.post(
            "/api/v1/plugins", json={"source": str(plugin_link)}
        )

    assert {kind: response.status_code for kind, response in responses.items()} == {
        "skill": 422,
        "agent": 422,
        "plugin": 422,
    }


@pytest.mark.asyncio
async def test_plugin_resources_expose_members_and_preserve_package_lifecycle(
    tmp_path: Path,
) -> None:
    """Plugin CRUD must use the package manager and keep member provenance."""

    source = _plugin(tmp_path / "sources")
    async with _api(tmp_path / "runtime") as (client, _):
        created = await client.post(
            "/api/v1/plugins", json={"source": str(source)}
        )
        assert created.status_code == 201, created.text
        view = created.json()["data"]
        assert view["id"] == "vendor.bundle"
        assert view["members"] == [
            {
                "id": "vendor.bundle/review",
                "plugin_id": "vendor.bundle",
                "kind": "skill",
                "name": "review",
                "version": "1.0.0",
            }
        ]
        assert (await client.get("/api/v1/plugins")).json()["data"] == [view]
        assert (
            await client.get("/api/v1/plugins/vendor.bundle")
        ).json()["data"] == view

        removed = await client.delete("/api/v1/plugins/vendor.bundle")
        assert removed.status_code == 204
        assert (
            await client.get("/api/v1/plugins/vendor.bundle")
        ).status_code == 404


@pytest.mark.asyncio
async def test_effective_plugin_resource_reports_runtime_state_and_provenance(
    tmp_path: Path,
) -> None:
    source = _plugin(tmp_path / "sources")
    async with _api(tmp_path / "runtime") as (client, runtime):
        created = await client.post("/api/v1/plugins", json={"source": str(source)})
        assert created.status_code == 201, created.text

        response = await client.get("/api/v1/plugins/effective")

        assert response.status_code == 200
        view = response.json()["data"][0]
        assert view["id"] == "vendor.bundle"
        assert view["version"] == "1.0.0"
        assert view["generation_id"] == runtime.plugin_runtime.current_generation.id
        assert view["state"] == "running"
        assert view["contributions"]["skills"] == ["vendor.bundle/review"]
        assert view["config"] == {}
        assert view["provenance"] == {}


@pytest.mark.asyncio
async def test_control_role_effective_resource_reports_installed_metadata(
    tmp_path: Path,
) -> None:
    source = _plugin(tmp_path / "sources")
    async with _api(tmp_path / "runtime") as (client, runtime):
        created = await client.post("/api/v1/plugins", json={"source": str(source)})
        assert created.status_code == 201, created.text
        runtime.role = "control"

        response = await client.get("/api/v1/plugins/effective")

        assert response.status_code == 200
        view = response.json()["data"][0]
        assert view["id"] == "vendor.bundle"
        assert view["generation_id"] is None
        assert view["state"] == "configured"
        assert view["contributions"]["skills"] == ["vendor.bundle/review"]


@pytest.mark.asyncio
async def test_plugin_api_upgrades_and_rolls_back_versions(tmp_path: Path) -> None:
    first = _plugin(tmp_path / "first", version="1.0.0")
    second = _plugin(tmp_path / "second", version="2.0.0")
    async with _api(tmp_path / "runtime") as (client, _runtime):
        assert (
            await client.post("/api/v1/plugins", json={"source": str(first)})
        ).status_code == 201

        upgraded = await client.post(
            "/api/v1/plugins",
            json={"source": str(second), "upgrade": True},
        )
        rolled_back = await client.post(
            "/api/v1/plugins/vendor.bundle/rollback"
        )

    assert upgraded.status_code == 201, upgraded.text
    assert upgraded.json()["data"]["version"] == "2.0.0"
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["data"]["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_referenced_dynamic_capability_retains_or_rejects_by_kind(
    tmp_path: Path,
) -> None:
    """A Skill referenced by an Agent stays visible after the package is gone."""

    kind = "skill"
    field = "skills"
    capability_id = "review"
    source = _skill(tmp_path / "sources")
    async with _api(tmp_path / "runtime") as (client, runtime):
        runtime.skills.install(source)
        await runtime.plugin_runtime.reload_installed()
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
        payload = {
            "name": f"{kind.title()} Agent",
            "model": {"provider": "bound", "name": "chat"},
            field: [capability_id],
        }
        created = await client.post("/api/v1/agents", json=payload)
        assert created.status_code == 201
        agent_id = created.json()["data"]["id"]

        response = await client.delete(
            f"/api/v1/packages/{kind}/{capability_id}"
        )

        detail = await client.get(f"/api/v1/agents/{agent_id}")

    assert response.status_code == 204
    assert detail.json()["data"]["skills"] == [capability_id]
    assert detail.json()["data"]["available"] is True
    assert detail.json()["data"]["missing_powers"] == [f"skill:{capability_id}"]


@pytest.mark.asyncio
async def test_plugin_mcp_member_selector_blocks_plugin_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a Plugin cannot orphan an Agent that selects its MCP member."""

    source = _plugin(tmp_path / "source")
    mcp = source / "mcps" / "weather"
    mcp.mkdir(parents=True)
    (mcp / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": "fake"}),
        encoding="utf-8",
    )
    (source / "plugin.yaml").write_text(
        """\
schema_version: 1
name: vendor.bundle
version: 1.0.0
description: Package fixture bundle.
""",
        encoding="utf-8",
    )

    async def weather_tools(_self: MCPClient):
        return (
            MCPRemoteTool(
                "forecast",
                "Forecast",
                "Forecast weather",
                {"type": "object"},
            ),
        )

    async def start(_client):
        pass

    monkeypatch.setattr(MCPClient, "start", start)
    monkeypatch.setattr(MCPClient, "list_tools", weather_tools)
    async with _api(tmp_path / "runtime", mcps=("*",)) as (client, _runtime_instance):
        installed = await client.post(
            "/api/v1/plugins",
            json={"source": str(source)},
        )
        assert installed.status_code == 201, installed.text
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
        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "Plugin MCP Agent",
                "model": {"provider": "bound", "name": "chat"},
                "mcps": ["vendor.bundle/weather"],
            },
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["data"]["id"]
        conflict = await client.delete("/api/v1/plugins/vendor.bundle")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"]["agent_ids"] == [agent_id]


@pytest.mark.asyncio
async def test_referenced_plugin_member_prevents_plugin_deletion(tmp_path: Path) -> None:
    """Plugin removal must account for Agents selecting any owned member."""

    source = _plugin(tmp_path / "sources")
    async with _api(tmp_path / "runtime") as (client, _):
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
        assert (
            await client.post("/api/v1/plugins", json={"source": str(source)})
        ).status_code == 201
        created = await client.post(
            "/api/v1/agents",
            json={
                "name": "Plugin Agent",
                "model": {"provider": "bound", "name": "chat"},
                "skills": ["vendor.bundle/review"],
            },
        )
        assert created.status_code == 201
        agent_id = created.json()["data"]["id"]

        conflict = await client.delete("/api/v1/plugins/vendor.bundle")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "plugin_in_use"
    assert "正在被专家使用" in conflict.json()["error"]["message"]
    assert "Plugin Agent" in conflict.json()["error"]["message"]
    assert "请先在“专家”页" in conflict.json()["error"]["message"]
    assert conflict.json()["error"]["details"] == {
        "kind": "plugin",
        "capability_id": "vendor.bundle",
        "agent_ids": [agent_id],
    }


@pytest.mark.asyncio
async def test_system_plugin_delete_reports_a_named_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A packaged Plugin cannot be uninstalled, and the refusal says why.

    The manager raises ``ValueError``, which used to render as
    ``422 validation_error`` carrying developer-facing English ("system Plugin
    cannot be uninstalled"): the console offers a Delete button for these
    Plugins, so the user saw an unexplainable failure.
    """

    system_root = tmp_path / "system"
    monkeypatch.setenv("BIANBU_SYSTEM_RESOURCE_ROOT", str(system_root))
    packaged = _plugin(system_root / "sources")
    shutil.copytree(packaged, system_root / "plugins" / "vendor.bundle")

    async with _api(tmp_path / "runtime") as (client, _):
        conflict = await client.delete("/api/v1/plugins/vendor.bundle")

    assert conflict.status_code == 409
    assert conflict.json()["error"] == {
        "code": "plugin_managed_by_system",
        "message": (
            "插件 vendor.bundle 由系统包管理，随 bianbu-agent-runtime 一起安装，"
            "无法在这里卸载。"
        ),
        "details": {"plugin_id": "vendor.bundle"},
    }


@pytest.mark.asyncio
async def test_plugin_activation_and_delete_reload_failures_roll_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activation_source = _plugin(tmp_path / "activation-source")
    async with _api(tmp_path / "activation-runtime") as (client, runtime):
        async def fail_activation():
            raise RuntimeError("candidate rejected")

        monkeypatch.setattr(runtime.plugin_runtime, "reload_installed", fail_activation)
        failed = await client.post(
            "/api/v1/plugins",
            json={"source": str(activation_source)},
        )

        assert failed.status_code == 409
        assert failed.json()["error"]["code"] == "plugin_activation_failed"
        assert failed.json()["error"]["details"] == {
            "plugin_id": "vendor.bundle",
            "error_type": "RuntimeError",
            "rolled_back": True,
        }
        assert not (runtime.home.plugins_dir / "vendor.bundle").exists()

    deletion_source = _plugin(tmp_path / "deletion-source")
    async with _api(tmp_path / "deletion-runtime") as (client, runtime):
        installed = await client.post(
            "/api/v1/plugins",
            json={"source": str(deletion_source)},
        )
        assert installed.status_code == 201
        original_reload = runtime.plugin_runtime.reload_installed
        attempts = 0

        async def fail_once():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("delete rejected")
            return await original_reload()

        monkeypatch.setattr(runtime.plugin_runtime, "reload_installed", fail_once)
        failed = await client.delete("/api/v1/plugins/vendor.bundle")

        assert failed.status_code == 409
        assert failed.json()["error"]["code"] == "plugin_reload_failed"
        assert failed.json()["error"]["details"] == {
            "plugin_id": "vendor.bundle",
            "error_type": "RuntimeError",
            "rolled_back": True,
        }
        assert attempts == 2
        assert (runtime.home.plugins_dir / "vendor.bundle").is_dir()
        assert "vendor.bundle" in runtime.plugin_runtime.current_generation.plugins


@pytest.mark.asyncio
async def test_plugin_activation_and_rollback_errors_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Secret-bearing Plugin exceptions must not cross the response or log boundary."""

    source = _plugin(tmp_path / "secret-source")
    secret = "plugin-token=do-not-disclose"
    rollback_secret = "rollback-password=also-secret"
    async with _api(tmp_path / "secret-runtime") as (client, runtime):
        async def fail_activation():
            raise RuntimeError(secret)

        def fail_rollback(_plugin_id: str) -> bool:
            raise OSError(rollback_secret)

        monkeypatch.setattr(runtime.plugin_runtime, "reload_installed", fail_activation)
        monkeypatch.setattr(runtime.plugins, "discard_install", fail_rollback)
        with caplog.at_level("ERROR"):
            response = await client.post(
                "/api/v1/plugins", json={"source": str(source)}
            )

    rendered = response.text + caplog.text
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "plugin_activation_failed",
        "message": "Plugin vendor.bundle could not be activated in the active runtime.",
        "details": {
            "plugin_id": "vendor.bundle",
            "error_type": "RuntimeError",
            "rolled_back": False,
        },
    }
    assert secret not in rendered
    assert rollback_secret not in rendered
