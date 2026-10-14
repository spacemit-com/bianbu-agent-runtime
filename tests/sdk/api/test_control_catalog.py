from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bianbu_agent.tools.builtins import BUILTIN_MODULES
from bianbu_agent.tools.summaries import SUMMARY_LANGUAGES

from bianbu_agent import (
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    RuntimeConfig,
)
from bianbu_agent.agents.manager import AgentManager
from bianbu_agent.config import RuntimePresetsConfig
from bianbu_agent.server.app import create_app
from tests.sdk.api.test_packages import _plugin, _skill


@pytest.fixture(autouse=True)
def isolated_user_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A real user's installed Skills must not become fixtures in catalog tests.
    monkeypatch.setenv("BIANBU_USER_SKILLS_DIR", str(tmp_path / "user-skills"))


@pytest.mark.asyncio
async def test_control_lists_builtin_tool_without_inventory_or_factory_execution(tmp_path):
    home = tmp_path / "runtime"
    # Tool 只能来自内置代码：遗留的用户 tool 目录既不能进目录，也不能被执行。
    tool = home / "tools" / "hostile"
    tool.mkdir(parents=True)
    (tool / "tool.py").write_text("raise RuntimeError('Control executed Tool code')\n")
    runtime = BianbuAgentRuntime(config=RuntimeConfig(data_dir=home),
                                default_model=None, agent_id="default", role="control")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=create_app(runtime, manage_lifespan=False)), base_url="http://test") as client:
            response = await client.get("/api/v1/capabilities/tool")
        assert response.status_code == 200, response.text
        assert {item["id"] for item in response.json()["data"]} == set(BUILTIN_MODULES)
        assert runtime.plugin_runtime.current_generation.tools == {}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_control_builtin_tools_ship_localized_summaries(tmp_path):
    # 控制台和 Bibit 插件都按界面语言取简介，所以 Control 目录必须带上双语
    # 简介；只有 Runtime 自有的内置工具有简介，技能等其它能力没有。
    home = tmp_path / "runtime"
    runtime = BianbuAgentRuntime(config=RuntimeConfig(data_dir=home),
                                default_model=None, agent_id="default", role="control")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=create_app(runtime, manage_lifespan=False)), base_url="http://test") as client:
            tools = (await client.get("/api/v1/capabilities/tool")).json()["data"]
            skills = (await client.get("/api/v1/capabilities/skill")).json()["data"]
    finally:
        await runtime.close()
    assert [item["id"] for item in tools]  # 设备一定带内置工具
    for item in tools:
        summaries = item["summaries"]
        assert set(summaries) == set(SUMMARY_LANGUAGES), item["id"]
        assert all(summaries[language].strip() for language in SUMMARY_LANGUAGES), item["id"]
    for item in skills:
        assert item["summaries"] == {}, item["id"]


@pytest.mark.asyncio
async def test_control_marks_the_configured_preset_capabilities(tmp_path):
    home = tmp_path / "runtime"
    config = RuntimeConfig(
        data_dir=home,
        presets=RuntimePresetsConfig(
            tools=("read_file", "terminal"),
            skills=("using-apm",),
        ),
    )
    runtime = BianbuAgentRuntime(config=config, default_model=None, agent_id="default", role="control")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=create_app(runtime, manage_lifespan=False)), base_url="http://test") as client:
            tools = {item["id"]: item for item in (await client.get("/api/v1/capabilities/tool")).json()["data"]}
            skills = {item["id"]: item for item in (await client.get("/api/v1/capabilities/skill")).json()["data"]}
        assert tools["read_file"]["preset"] is True
        assert tools["terminal"]["preset"] is True
        assert tools["write_file"]["preset"] is False
        # A preset Skill is marked the same way; a device that does not ship it
        # simply has no entry.
        assert skills.get("using-apm", {}).get("preset") in {None, True}
        assert all(item["preset"] is False for item in skills.values())
    finally:
        await runtime.close()


class _WorkerClient:
    def __init__(self) -> None:
        self.reload_calls = 0

    async def catalog(self):
        return {
            "generation_id": 7,
            "capabilities": {
                "tool": [{
                    "id": "vendor.bundle/echo", "name": "vendor.bundle/echo",
                    "display_name": "echo", "description": "Echo.",
                    "name_zh": "",
                    "name_en": "",
                    "description_zh": "",
                    "summaries": {},
                    "version": "1.0.0", "origin": "plugin",
                    "plugin_id": "vendor.bundle", "enabled": True,
                    "assignable": True, "unavailable_reason": "",
                }],
                "skill": [{
                    "id": "vendor.bundle/review", "name": "vendor.bundle/review",
                    "display_name": "review", "description": "Review.",
                    "name_zh": "",
                    "name_en": "",
                    "description_zh": "",
                    "summaries": {},
                    "version": "1.0.0", "origin": "plugin",
                    "plugin_id": "vendor.bundle", "enabled": True,
                    "assignable": True, "unavailable_reason": "",
                }],
                "hook": [{
                    "id": "vendor.bundle/audit", "name": "vendor.bundle/audit",
                    "display_name": "audit", "description": "",
                    "name_zh": "",
                    "name_en": "",
                    "description_zh": "",
                    "summaries": {},
                    "version": "1.0.0", "origin": "plugin",
                    "plugin_id": "vendor.bundle", "enabled": True,
                    "assignable": True, "unavailable_reason": "",
                }],
            },
        }

    async def reload(self):
        self.reload_calls += 1
        return {"status": "active", "generation_id": 8 + self.reload_calls, "changed": True}

@pytest.mark.asyncio
async def test_control_skill_signature_rejection_is_forbidden(tmp_path, monkeypatch):
    from bianbu_agent.security.security_codesign import CodeSignatureRejected
    def reject(path, kind):
        raise CodeSignatureRejected(kind, path, "signature rejected")
    monkeypatch.setattr("bianbu_agent.security.security_codesign.enforce_code_signature", reject)
    runtime = BianbuAgentRuntime(config=RuntimeConfig(data_dir=tmp_path / "runtime"),
                                default_model=None, agent_id="default", role="control")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=create_app(runtime, manage_lifespan=False)), base_url="http://test") as client:
            response = await client.post("/api/v1/packages/skill", json={
                "source": str(_skill(tmp_path / "source")),
            })
        assert response.status_code == 403, response.text
        assert "signature rejected" in response.text
        assert runtime.skills.inspect("review") is None
    finally:
        await runtime.close()


class _Supervisor:
    def __init__(self) -> None:
        self.client = _WorkerClient()

    @property
    def ready(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_control_api_exposes_plugin_skill_with_its_qualified_id(
    tmp_path: Path,
) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    runtime.plugins.install(_plugin(tmp_path / "source"))
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/capabilities/skill")
    finally:
        await runtime.close()

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "id": "vendor.bundle/review",
            "name": "vendor.bundle/review",
            "display_name": "review",
            "description": "Review carefully",
            "name_zh": "",
            "name_en": "",
            "description_zh": "",
            "summaries": {},
            "version": "1.0.0",
            "origin": "plugin",
            "plugin_id": "vendor.bundle",
            "enabled": True,
            "assignable": True,
            "preset": False,
            "unavailable_reason": "",
        },
    ]
    # ControlPluginFacade remains empty: only Worker loaded the catalog.
    assert runtime.plugin_runtime.current_generation.plugins == {}


@pytest.mark.asyncio
async def test_control_agent_patch_validates_selected_skill_against_control_catalog(
    tmp_path: Path,
) -> None:
    """Agent edits use the same Control-owned catalog shown by the API."""

    registry = InMemoryAgentRegistry((
        AgentSpec(id="default", name="Default"),
        AgentSpec(
            id="editor",
            name="Editor",
            skills=("vendor.bundle/review",),
        ),
    ))
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=object(),
        agent_id="default",
        agent_registry=registry,
        role="control",
    )
    runtime.plugins.install(_plugin(tmp_path / "source"))
    runtime.agents = AgentManager(
        store=registry,
        model_registry=runtime._models,
        generation_provider=lambda: runtime.plugin_runtime.current_generation,
    )
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.patch(
                "/api/v1/agents/editor",
                json={
                    "name": "Edited",
                    "skills": ["vendor.bundle/review"],
                },
            )
    finally:
        await runtime.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"]["skills"] == ["vendor.bundle/review"]


@pytest.mark.asyncio
async def test_control_agent_patch_does_not_query_worker_catalog(
    tmp_path: Path,
) -> None:
    """Worker catalog failures do not affect Control-owned expert metadata."""

    registry = InMemoryAgentRegistry((
        AgentSpec(id="default", name="Default"),
        AgentSpec(id="editor", name="Editor"),
    ))
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=object(),
        agent_id="default",
        agent_registry=registry,
        role="control",
    )
    runtime.agents = AgentManager(
        store=registry,
        model_registry=runtime._models,
        generation_provider=lambda: runtime.plugin_runtime.current_generation,
    )
    runtime.worker_supervisor = _Supervisor()
    runtime.worker_supervisor.for_agent = lambda agent_id, **kwargs: runtime.worker_supervisor
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.patch(
                "/api/v1/agents/editor",
                json={"name": "Edited"},
            )
    finally:
        await runtime.close()

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_control_plugin_mutations_reload_worker_generation(tmp_path: Path) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    supervisor = _Supervisor()
    runtime.worker_supervisor = supervisor
    app = create_app(runtime, manage_lifespan=False)
    source = _plugin(tmp_path / "source")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post("/api/v1/plugins", json={"source": str(source)})
            assert created.status_code == 201, created.text
            assert (await client.get("/api/v1/plugins")).json()["data"][0]["id"] == "vendor.bundle"
            removed = await client.delete("/api/v1/plugins/vendor.bundle")
            assert removed.status_code == 204, removed.text
    finally:
        await runtime.close()

    assert supervisor.client.reload_calls == 2
    assert runtime.plugin_runtime.current_generation.plugins == {}


@pytest.mark.asyncio
async def test_control_mcp_mutations_reload_and_read_worker_catalog(tmp_path: Path) -> None:
    """The control API must activate MCP packages without loading them locally."""

    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )

    class MCPWorkerClient(_WorkerClient):
        async def catalog(self):
            present = runtime.mcps.inspect("mcp.control") is not None
            return {
                "generation_id": 20 + self.reload_calls,
                "capabilities": {"tool": [], "skill": [], "hook": []},
                "mcps": ["mcp.control"] if present else [],
                "mcp_servers": ([{
                    "id": "mcp.control",
                    "display_name": "Control",
                    "command": "fake",
                    "kind": "mcp",
                    "status": "healthy",
                    "transport": "stdio",
                    "tools": [],
                    "source_kind": "standalone",
                    "owner_plugin_id": None,
                    "removable": True,
                    "health_checked_at": None,
                }] if present else []),
            }

    supervisor = _Supervisor()
    supervisor.client = MCPWorkerClient()
    runtime.worker_supervisor = supervisor
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            installed = await client.post(
                "/api/v1/mcp-servers",
                json={"name": "Control", "command": "fake"},
            )
            listing = await client.get("/api/v1/mcp-servers")
            removed = await client.delete(
                "/api/v1/mcp-servers", params={"id": "mcp.control"}
            )
    finally:
        await runtime.close()

    assert installed.status_code == 201, installed.text
    assert installed.json()["data"]["id"] == "mcp.control"
    assert installed.json()["data"]["status"] == "healthy"
    assert listing.json()["data"][0]["status"] == "healthy"
    assert [item["id"] for item in listing.json()["data"]] == ["mcp.control"]
    assert removed.status_code == 204, removed.text
    assert supervisor.client.reload_calls == 2
    assert runtime.plugin_runtime.current_generation.mcps == {}


@pytest.mark.asyncio
async def test_control_mcp_install_answers_while_the_worker_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Worker that cannot answer ``reload`` yet must not freeze MCP mutations.

    K3 实测：Worker 给一个新的 ``npx`` MCP 首次固化 npm 包时，``reload`` 几分钟
    不回话。安装陪着一起等，``catalog_mutation_lock`` 就一直被占着——安装请求
    永不返回，后面所有 MCP 增删也一起排队，新装的 Server 永远不进列表。
    """
    from bianbu_agent.api import packages as packages_api

    monkeypatch.setattr(packages_api, "WORKER_ACTIVATION_TIMEOUT_SECONDS", 0.05)
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )

    class BusyWorkerClient(_WorkerClient):
        def __init__(self) -> None:
            super().__init__()
            self.released = asyncio.Event()

        async def reload(self):
            self.reload_calls += 1
            await self.released.wait()
            return {"status": "active", "generation_id": 30 + self.reload_calls, "changed": True}

        async def catalog(self):
            return {
                "generation_id": 30,
                "capabilities": {"tool": [], "skill": [], "hook": []},
                "mcps": [],
                "mcp_servers": [],
            }

    supervisor = _Supervisor()
    supervisor.client = BusyWorkerClient()
    runtime.worker_supervisor = supervisor
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            started = time.monotonic()
            installed = await client.post(
                "/api/v1/mcp-servers",
                json={"name": "Busy", "command": "npx -y busy-mcp"},
            )
            install_seconds = time.monotonic() - started
            listing = await client.get("/api/v1/mcp-servers")
            removed = await client.delete(
                "/api/v1/mcp-servers", params={"id": "mcp.busy"}
            )
    finally:
        supervisor.client.released.set()
        await asyncio.sleep(0.05)
        await runtime.close()

    assert installed.status_code == 201, installed.text
    assert installed.json()["data"]["id"] == "mcp.busy"
    assert [item["id"] for item in listing.json()["data"]] == ["mcp.busy"]
    assert removed.status_code == 204, removed.text
    # 等待有上限：装完立刻能列出，也不耽误紧随其后的删除。
    assert install_seconds < 2.0
    assert supervisor.client.reload_calls == 2


@pytest.mark.asyncio
async def test_control_catalog_returns_builtin_tools_without_a_worker(
    tmp_path: Path,
) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )

    class Down:
        ready = False
        client = None

    runtime.worker_supervisor = Down()
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/capabilities/tool")
    finally:
        await runtime.close()

    assert response.status_code == 200
    assert {item["id"] for item in response.json()["data"]} == set(BUILTIN_MODULES)


@pytest.mark.asyncio
async def test_control_catalog_reads_installed_skill_without_a_worker(
    tmp_path: Path,
) -> None:
    """Expert configuration reads the Control-owned static Skill directory."""

    skill = tmp_path / ".bianbu-agent" / "skills" / "release-notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: release-notes\ndisplay_name: Release notes\n"
        "description: Write concise release notes\nversion: 1.2.3\n---\n"
        "Use the repository history.\n",
        encoding="utf-8",
    )
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )

    class Down:
        ready = False
        client = None

    runtime.worker_supervisor = Down()
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/capabilities/skill")
    finally:
        await runtime.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"] == [{
        "id": "release-notes",
        "name": "release-notes",
        "display_name": "Release notes",
        "description": "Write concise release notes",
        "name_zh": "",
        "name_en": "",
        "description_zh": "",
        "summaries": {},
        "version": "1.2.3",
        "origin": "standalone",
        "plugin_id": None,
        "enabled": True,
        "assignable": True,
        "preset": False,
        "unavailable_reason": "",
    }]


@pytest.mark.asyncio
async def test_control_lists_installed_mcp_without_a_worker(tmp_path: Path) -> None:
    """MCP selection metadata is also available before a Worker starts."""

    mcp = tmp_path / ".bianbu-agent" / "mcps" / "mcp.notes"
    mcp.mkdir(parents=True)
    (mcp / "mcp.json").write_text(
        '{"display_name":"Notes","transport":"stdio","command":"notes-mcp"}',
        encoding="utf-8",
    )
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/mcp-servers")
    finally:
        await runtime.close()

    assert response.status_code == 200, response.text
    assert response.json()["data"] == [{
        "id": "mcp.notes",
        "display_name": "Notes",
        "command": "notes-mcp",
        "kind": "mcp",
        "status": "not_loaded",
        # No Worker could be asked, so the lifecycle says exactly that instead
        # of repeating the Control catalog's ``not_loaded``: "unknown" (nobody
        # reported) and "declared but never loaded" are different answers and
        # the UI explains them differently.
        "state": "unknown",
        "phase": "",
        "ready": False,
        "tools_count": 0,
        "transport": "stdio",
        "tools": [],
        "source_kind": "standalone",
        "owner_plugin_id": None,
        "removable": True,
        "health_checked_at": None,
        "detail": "",
    }]


@pytest.mark.asyncio
async def test_control_installs_skill_without_starting_a_worker(tmp_path: Path) -> None:
    source = _skill(tmp_path / "source", "summarize")
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            installed = await client.post(
                "/api/v1/packages/skill",
                json={"source": str(source)},
            )
            catalog = await client.get("/api/v1/capabilities/skill")
    finally:
        await runtime.close()

    assert installed.status_code == 201, installed.text
    assert [item["id"] for item in catalog.json()["data"]] == ["summarize"]


@pytest.mark.asyncio
async def test_control_installs_mcp_without_starting_a_worker(tmp_path: Path) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            installed = await client.post(
                "/api/v1/mcp-servers",
                json={"name": "Notes", "command": "notes-mcp --stdio"},
            )
            catalog = await client.get("/api/v1/mcp-servers")
    finally:
        await runtime.close()

    assert installed.status_code == 201, installed.text
    assert installed.json()["data"]["status"] == "not_loaded"
    assert [item["id"] for item in catalog.json()["data"]] == ["mcp.notes"]


@pytest.mark.asyncio
async def test_control_updates_skill_without_a_worker(tmp_path: Path) -> None:
    first = _skill(tmp_path / "v1", "summarize")
    second = _skill(tmp_path / "v2", "summarize")
    manifest = second / "SKILL.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        .replace("Review carefully", "Summarize precisely")
        .replace("1.0.0", "2.0.0"),
        encoding="utf-8",
    )
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post(
                "/api/v1/packages/skill", json={"source": str(first)}
            )).status_code == 201
            updated = await client.post(
                "/api/v1/packages/skill",
                json={"source": str(second), "upgrade": True},
            )
            catalog = await client.get("/api/v1/capabilities/skill")
    finally:
        await runtime.close()

    assert updated.status_code == 201, updated.text
    assert updated.json()["data"]["version"] == "2.0.0"
    assert catalog.json()["data"][0]["description"] == "Summarize precisely"


@pytest.mark.asyncio
async def test_control_retains_updated_skill_until_every_active_task_finishes(
    tmp_path: Path,
) -> None:
    first = _skill(tmp_path / "v1", "summarize")
    second = _skill(tmp_path / "v2", "summarize")
    manifest = second / "SKILL.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("1.0.0", "2.0.0"),
        encoding="utf-8",
    )
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    runtime.runs = SimpleNamespace(active_count=2)
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post(
                "/api/v1/packages/skill", json={"source": str(first)}
            )).status_code == 201
            assert (await client.post(
                "/api/v1/packages/skill",
                json={"source": str(second), "upgrade": True},
            )).status_code == 201
            assert (await client.delete(
                "/api/v1/packages/skill/summarize"
            )).status_code == 204
            assert (await client.get(
                "/api/v1/capabilities/skill"
            )).json() == {"data": []}

        retired = tuple(runtime.home.staging_dir.glob("retired-skill-*"))
        assert len(retired) == 2
        retained_versions = {
            "2.0.0" if "version: 2.0.0" in (item / "SKILL.md").read_text(
                encoding="utf-8"
            ) else "1.0.0"
            for item in retired
        }
        assert retained_versions == {"1.0.0", "2.0.0"}
        runtime.runs.active_count = 1
        assert runtime.cleanup_retired_packages() == 0
        assert all(item.is_dir() for item in retired)
        runtime.runs.active_count = 0
        assert runtime.cleanup_retired_packages() == 2
        assert all(not item.exists() for item in retired)
    finally:
        del runtime.runs
        await runtime.close()


@pytest.mark.asyncio
async def test_control_updates_mcp_without_a_worker(tmp_path: Path) -> None:
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        role="control",
    )
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post(
                "/api/v1/mcp-servers",
                json={"name": "Notes", "command": "notes-mcp --old"},
            )).status_code == 201
            updated = await client.post(
                "/api/v1/mcp-servers",
                json={
                    "name": "Notes",
                    "command": "notes-mcp --new",
                    "upgrade": True,
                },
            )
    finally:
        await runtime.close()

    assert updated.status_code == 201, updated.text
    assert updated.json()["data"]["command"] == "notes-mcp --new"
    installed = (
        tmp_path / ".bianbu-agent" / "mcps" / "mcp.notes" / "mcp.json"
    ).read_text(encoding="utf-8")
    assert '"--new"' in installed
    assert '"--old"' not in installed


@pytest.mark.asyncio
async def test_skill_delete_keeps_agent_reference_and_removes_catalog_entry(
    tmp_path: Path,
) -> None:
    source = _skill(tmp_path / "source", "summarize")
    registry = InMemoryAgentRegistry((
        AgentSpec(id="default", name="Default"),
        AgentSpec(id="editor", name="Editor", skills=("summarize",)),
    ))
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=object(),
        agent_id="default",
        agent_registry=registry,
        role="control",
    )
    runtime.agents = AgentManager(
        store=registry,
        model_registry=runtime._models,
        generation_provider=lambda: runtime.plugin_runtime.current_generation,
    )
    runtime.skills.install(source)
    app = create_app(runtime, manage_lifespan=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            deleted = await client.delete("/api/v1/packages/skill/summarize")
            catalog = await client.get("/api/v1/capabilities/skill")
            edited = await client.patch(
                "/api/v1/agents/editor",
                json={"description": "Still references the removed Skill"},
            )
            rejected = await client.patch(
                "/api/v1/agents/editor",
                json={"skills": ["summarize", "never-installed"]},
            )
    finally:
        retained = await registry.get("editor")
        await runtime.close()

    assert deleted.status_code == 204, deleted.text
    assert catalog.json() == {"data": []}
    assert edited.status_code == 200, edited.text
    assert edited.json()["data"]["skills"] == ["summarize"]
    # The retained reference is reported as a gap, not as a broken Agent: this
    # Agent is unavailable only because the fixture gives it no model.
    assert edited.json()["data"]["available"] is False
    assert edited.json()["data"]["unavailable_reason"] == "agent_model_required"
    assert edited.json()["data"]["missing_powers"] == ["skill:summarize"]
    assert rejected.status_code == 422
    assert retained.skills == ("summarize",)


@pytest.mark.asyncio
async def test_worker_inventory_only_enables_selected_mcps(tmp_path):
    from dataclasses import replace
    from bianbu_agent.mcp.managed import parse_managed_mcp, write_managed_mcp_package

    selected = 'mcp.selected'
    registry = InMemoryAgentRegistry()
    await registry.create(AgentSpec(id='default', name='Default', mcps=[selected]))
    runtime = BianbuAgentRuntime(config=RuntimeConfig(data_dir=tmp_path / 'runtime'),
        default_model=None, agent_id='default', agent_registry=registry)
    try:
        for name in ('selected', 'unselected'):
            package = write_managed_mcp_package(parse_managed_mcp(name, '/bin/false'),
                                                tmp_path)
            runtime.mcps.install(package)
        snapshot = await runtime._execution_inventory_snapshot()
        enabled = {item.source.capability_id for item in snapshot.items
                   if item.source.kind.value == 'mcp' and item.enabled}
        assert enabled == {selected}
        original = await registry.get('default')
        await registry.update(replace(original, mcps=('*',)))
        snapshot = await runtime._execution_inventory_snapshot()
        assert {item.source.capability_id for item in snapshot.items
                if item.source.kind.value == 'mcp' and item.enabled} == {
                    selected, 'mcp.unselected'}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_worker_inventory_matches_an_apm_mcp_name(tmp_path):
    """An expert that declares the APM name ``nws`` still reaches ``mcp.nws``."""
    from bianbu_agent.mcp.managed import parse_remote_mcp, write_managed_mcp_package

    registry = InMemoryAgentRegistry()
    await registry.create(AgentSpec(id='default', name='Default', mcps=['nws']))
    runtime = BianbuAgentRuntime(config=RuntimeConfig(data_dir=tmp_path / 'runtime'),
        default_model=None, agent_id='default', agent_registry=registry)
    try:
        package = write_managed_mcp_package(
            parse_remote_mcp('nws', 'https://example.test/mcp'), tmp_path)
        runtime.mcps.install(package)
        snapshot = await runtime._execution_inventory_snapshot()
        enabled = {item.source.capability_id for item in snapshot.items
                   if item.source.kind.value == 'mcp' and item.enabled}
        assert enabled == {'mcp.nws'}
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('other,expected', [('healthy', 'healthy'), ('unhealthy', 'degraded'), ('unknown', 'degraded')])
async def test_control_merges_all_worker_mcp_states_without_changing_inventory(monkeypatch, other, expected):
    from bianbu_agent.api.mcp_servers import _control_server_views
    base = dict(id='weather', display_name='Weather', command='trusted-command', kind='mcp',
                status='not_loaded', transport='stdio', tools=[], source_kind='system',
                owner_plugin_id=None, removable=False, health_checked_at=None)
    monkeypatch.setattr('bianbu_agent.api.mcp_servers.control_catalog_snapshot',
                        lambda _: {'mcp_servers': [base]})
    async def catalogs():
        second = {'error': 'offline'} if other == 'unknown' else {
            'catalog': {'mcp_servers': [dict(base, status=other)]}}
        return [{'catalog': {'mcp_servers': [dict(base, command='untrusted-command',
                    removable=True, status='healthy'), dict(base, id='deleted', status='healthy')]}}, second]
    runtime = SimpleNamespace(worker_supervisor=SimpleNamespace(catalogs=catalogs))
    views = await _control_server_views(runtime)
    assert len(views) == 1
    assert views[0].status == expected
    assert views[0].command == 'trusted-command'
    assert views[0].removable is False


@pytest.mark.asyncio
async def test_control_mcp_view_explains_a_preparation_in_flight(monkeypatch):
    """A long first start has to say which wait the operator is in.

    The K3 install of ``bilibili-mcp-js`` downloads 116MB and then handshakes;
    reporting only ``preparing`` for both left the UI stuck on a word that
    never changed, so the phase and the readiness flag travel with the view.
    """

    from bianbu_agent.api.mcp_servers import _control_server_views
    base = dict(id='bilibili-search', display_name='bilibili-search',
                command='npx bilibili-mcp-js', kind='mcp', status='not_loaded',
                transport='stdio', tools=[], source_kind='standalone',
                owner_plugin_id=None, removable=True, health_checked_at=None)
    monkeypatch.setattr('bianbu_agent.api.mcp_servers.control_catalog_snapshot',
                        lambda _: {'mcp_servers': [base]})

    async def catalogs():
        return [{'catalog': {
            'mcp_servers': [dict(base, status='preparing')],
            'mcp_statuses': {'bilibili-search': {
                'state': 'preparing', 'phase': 'installing', 'reason': '',
            }},
        }}]

    views = await _control_server_views(
        SimpleNamespace(worker_supervisor=SimpleNamespace(catalogs=catalogs))
    )
    assert views[0].status == 'preparing'
    assert views[0].state == 'preparing'
    assert views[0].phase == 'installing'
    assert views[0].ready is False
    assert views[0].tools_count == 0


@pytest.mark.asyncio
async def test_control_mcp_view_counts_the_tools_of_a_ready_server(monkeypatch):
    from bianbu_agent.api.mcp_servers import _control_server_views
    base = dict(id='bilibili-search', display_name='bilibili-search',
                command='npx bilibili-mcp-js', kind='mcp', status='not_loaded',
                transport='stdio', tools=[], source_kind='standalone',
                owner_plugin_id=None, removable=True, health_checked_at=None)
    monkeypatch.setattr('bianbu_agent.api.mcp_servers.control_catalog_snapshot',
                        lambda _: {'mcp_servers': [base]})
    tools = [{'id': 'bilibili-search/search', 'name': 'search', 'display_name': 'search'},
             {'id': 'bilibili-search/detail', 'name': 'detail', 'display_name': 'detail'}]

    async def catalogs():
        return [{'catalog': {
            'mcp_servers': [dict(base, status='healthy', tools=tools)],
            # A settled preparation carries no phase: a stale "installing" on a
            # server that already answered is exactly what misleads here.
            'mcp_statuses': {'bilibili-search': {
                'state': 'ready', 'phase': '', 'reason': '',
            }},
        }}]

    views = await _control_server_views(
        SimpleNamespace(worker_supervisor=SimpleNamespace(catalogs=catalogs))
    )
    assert views[0].status == 'healthy'
    assert views[0].state == 'ready'
    assert views[0].phase == ''
    assert views[0].ready is True
    assert views[0].tools_count == 2
    # Tools come back sorted by id, which is what every list view shows.
    assert [tool.name for tool in views[0].tools] == ['detail', 'search']


@pytest.mark.asyncio
async def test_control_surfaces_mcp_load_failure(monkeypatch):
    from bianbu_agent.api.mcp_servers import _control_server_views
    monkeypatch.setattr('bianbu_agent.api.mcp_servers.control_catalog_snapshot', lambda _: {
        'mcp_servers': [dict(id='broken', display_name='Broken', command='fake', status='not_loaded',
                             transport='stdio', tools=[], source_kind='standalone', removable=True,
                             kind='mcp', owner_plugin_id=None, health_checked_at=None)]})
    async def catalogs():
        return [{'catalog': {'mcp_statuses': {'broken': {'state': 'failed', 'phase': '',
                                                         'reason': 'connection failed'}}}}]
    views = await _control_server_views(SimpleNamespace(worker_supervisor=SimpleNamespace(catalogs=catalogs)))
    assert views[0].status == 'unhealthy'
    assert views[0].state == 'failed'
    assert views[0].ready is False
    assert views[0].detail == 'connection failed'


@pytest.mark.asyncio
async def test_control_installs_command_hook_and_mcp_bundle_without_worker(tmp_path):
    import json
    source = tmp_path / 'example.commands'
    hook = source / 'hooks/example.check'; hook.mkdir(parents=True)
    (source / 'scripts').mkdir()
    (source / 'scripts/check.py').write_text('raise RuntimeError("Control executed command")')
    (hook / 'hook.json').write_text(json.dumps({'schema_version':1,'hooks':{
        'PreToolUse':[{'hooks':[{'type':'command','command':'python3 "${PLUGIN_ROOT}/scripts/check.py"'}]}]}}))
    mcp = source / 'mcps/server'; mcp.mkdir(parents=True)
    (mcp / 'mcp.json').write_text(json.dumps({'transport':'stdio','command':'must-not-execute'}))
    (source / 'plugin.yaml').write_text('schema_version: 1\nname: example.commands\nversion: 1.0.0\ndescription: Control catalog fixture.\n')
    runtime = BianbuAgentRuntime(config=RuntimeConfig(data_dir=tmp_path/'runtime'),
                                default_model=None, role='control')
    await runtime.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False)), base_url='http://test') as client:
            installed = await client.post('/api/v1/plugins', json={'source':str(source)})
            assert installed.status_code == 201, installed.text
            mcps = (await client.get('/api/v1/mcp-servers')).json()['data']
            assert [item['id'] for item in mcps] == ['example.commands/server']
            assert mcps[0]['owner_plugin_id'] == 'example.commands'
            assert mcps[0]['removable'] is False
            # Model-less expert setup still works while no execution Worker exists.
            created = await client.post('/api/v1/agents', json={'name':'Bundle', 'plugins':['example.commands']})
            assert created.status_code == 201, created.text
            assert created.json()['data']['plugins'] == ['example.commands']
            refused = await client.delete('/api/v1/plugins/example.commands')
            assert refused.status_code == 409
            agent_id = created.json()['data']['id']
            changed = await client.patch(f'/api/v1/agents/{agent_id}', json={'plugins':['*']})
            assert changed.status_code == 200, changed.text
            assert (await client.delete('/api/v1/plugins/example.commands')).status_code == 204
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_control_mcp_view_shows_the_tools_a_preparation_proved(monkeypatch):
    """A Server no Expert selected still has a real state and real tools.

    Its Generation never connects it, so the handshake the Runtime ran itself is
    the only evidence there is.  Reporting ``not_loaded`` for it is what left
    the operator with a word and no explanation.
    """

    from datetime import datetime, timezone

    from bianbu_agent.api.mcp_servers import _control_server_views
    base = dict(id='nws', display_name='nws', command='npx nws-mcp', kind='mcp',
                status='not_loaded', transport='stdio', tools=[],
                source_kind='standalone', owner_plugin_id=None, removable=True,
                health_checked_at=None)
    monkeypatch.setattr('bianbu_agent.api.mcp_servers.control_catalog_snapshot',
                        lambda _: {'mcp_servers': [base]})
    tools = [{'id': 'forecast', 'name': 'forecast', 'display_name': 'forecast'}]

    async def catalogs():
        return [{'catalog': {'mcp_statuses': {'nws': {
            'state': 'ready', 'phase': '', 'reason': '',
            'tools': tools, 'prepared_at': 1700000000.0, 'vendored': True,
        }}}}]

    views = await _control_server_views(
        SimpleNamespace(worker_supervisor=SimpleNamespace(catalogs=catalogs))
    )
    assert views[0].state == 'ready'
    assert views[0].ready is True
    assert views[0].status == 'healthy'
    assert [tool.name for tool in views[0].tools] == ['forecast']
    assert views[0].tools_count == 1
    assert views[0].health_checked_at == datetime.fromtimestamp(
        1700000000.0, tz=timezone.utc
    )


@pytest.mark.asyncio
async def test_control_mcp_view_repeats_why_a_preparation_failed(monkeypatch):
    from bianbu_agent.api.mcp_servers import _control_server_views
    monkeypatch.setattr('bianbu_agent.api.mcp_servers.control_catalog_snapshot', lambda _: {
        'mcp_servers': [dict(id='nws', display_name='nws', command='npx nws-mcp',
                             kind='mcp', status='not_loaded', transport='stdio', tools=[],
                             source_kind='standalone', owner_plugin_id=None,
                             removable=True, health_checked_at=None)]})

    async def catalogs():
        return [{'catalog': {'mcp_statuses': {'nws': {
            'state': 'failed', 'phase': '', 'reason': '',
            'detail': 'npm ERR! 404 Not Found',
        }}}}]

    views = await _control_server_views(
        SimpleNamespace(worker_supervisor=SimpleNamespace(catalogs=catalogs))
    )
    assert views[0].state == 'failed'
    assert views[0].detail == 'npm ERR! 404 Not Found'


def test_worker_catalog_reports_every_declared_server_with_its_preparation():
    """The Worker has to answer for servers no Generation loaded, too."""

    from types import SimpleNamespace

    from bianbu_agent.security.runtime_process.worker import _mcp_status_entries

    catalog = SimpleNamespace(mcp_sources={
        'nws': SimpleNamespace(capability_id='nws'),
        'loaded': SimpleNamespace(capability_id='loaded'),
        'broken': SimpleNamespace(capability_id='broken'),
    })
    generation = SimpleNamespace(statuses={
        'loaded': SimpleNamespace(health=SimpleNamespace(status='healthy'),
                                  diagnostics=()),
    })

    class Preparation:
        def state(self, mcp_id):
            return {
                'nws': SimpleNamespace(status='ready', phase='', detail='',
                                       tools=('forecast',), vendored=True,
                                       checked_at=1700000000.0),
                'loaded': None,
                'broken': SimpleNamespace(status='failed', phase='',
                                          detail='npm ERR! offline', tools=(),
                                          vendored=False, checked_at=None),
                'mcp.bilibili-search': SimpleNamespace(
                    status='ready', phase='', detail='', tools=('bilibili-hot',),
                    vendored=True, checked_at=1700000000.0),
            }[mcp_id]

    # A server nobody selected is not in the Generation and not in its catalog:
    # only the installed list can put it on the Control plane's map.
    entries = _mcp_status_entries(
        catalog, generation, Preparation(), ('mcp.bilibili-search',)
    )
    assert entries['mcp.bilibili-search']['state'] == 'ready'
    assert [tool['name'] for tool in entries['mcp.bilibili-search']['tools']] == [
        'bilibili-hot'
    ]
    assert entries['nws']['state'] == 'ready'
    assert [tool['name'] for tool in entries['nws']['tools']] == ['forecast']
    assert entries['nws']['prepared_at'] == 1700000000.0
    # A Server the Generation did load keeps the Generation's own health.
    assert entries['loaded']['state'] == 'ready'
    # Its tools come from the Generation's own ``mcp_servers`` entry, so the
    # status entry must not pretend to carry them.
    assert 'tools' not in entries['loaded']
    assert entries['broken']['state'] == 'failed'
    assert entries['broken']['detail'] == 'npm ERR! offline'


@pytest.mark.asyncio
async def test_mcp_mutation_warms_the_device_worker_when_none_is_running(monkeypatch):
    """An install must not wait for somebody to run an Expert."""

    from types import SimpleNamespace

    import bianbu_agent.provisioning as provisioning
    from bianbu_agent.api.mcp_servers import _warm_mcp_worker

    monkeypatch.setattr(provisioning, 'system_starter_id', lambda _data_dir: 'com.bianbu.bibit')
    warmed: list[str] = []
    supervisor = SimpleNamespace(ready=False, warm_agent=warmed.append)
    runtime = SimpleNamespace(role='control', worker_supervisor=supervisor,
                              config=SimpleNamespace(data_dir=Path('/data')))

    _warm_mcp_worker(runtime)
    assert warmed == ['com.bianbu.bibit']

    # One Worker already running is enough: the install publishes to it.
    supervisor.ready = True
    _warm_mcp_worker(runtime)
    assert warmed == ['com.bianbu.bibit']

    # A Worker-role Runtime prepares in its own process, so it warms nothing.
    supervisor.ready = False
    runtime.role = 'worker'
    _warm_mcp_worker(runtime)
    assert warmed == ['com.bianbu.bibit']


def test_worker_catalog_prefers_the_generation_that_actually_connected():
    """A probe that failed must not overwrite a Generation's live verdict.

    The Generation started the server and reported what it saw; the manager's
    background probe is a second, weaker witness for a server nobody connected.
    """

    from types import SimpleNamespace

    from bianbu_agent.security.runtime_process.worker import _mcp_status_entries

    catalog = SimpleNamespace(mcp_sources={
        'loaded': SimpleNamespace(capability_id='loaded'),
    })
    generation = SimpleNamespace(statuses={
        'loaded': SimpleNamespace(health=SimpleNamespace(status='healthy'),
                                  diagnostics=()),
    })

    class Preparation:
        def state(self, _mcp_id):
            return SimpleNamespace(status='failed', phase='', detail='probe died',
                                   tools=(), vendored=False, checked_at=None)

    entry = _mcp_status_entries(catalog, generation, Preparation())['loaded']
    assert entry == {'state': 'ready', 'phase': '', 'reason': ''}
