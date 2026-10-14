from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import shutil

import pytest

from bianbu_agent import (
    AgentManifest,
    AgentPackageManager,
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    RunEvent,
    RuntimeConfig,
)
from bianbu_agent.home import AgentHome
from bianbu_agent.agents.manager import AgentManager
from bianbu_agent.plugins import (
    HookPoint,
    PluginCatalog,
    PluginInstaller,
    RunContext,
)
from bianbu_agent.skills import DirectorySkillLoader, SkillPackageManager


PROJECT_ROOT = Path(__file__).parents[2]


def test_starter_agent_names_the_capabilities_it_ships_instead_of_a_wildcard() -> None:
    """The packaged Bibit expert declares ids, not `*`.

    A wildcard defers the meaning to whatever the Runtime happens to have
    installed, so every surface had to special-case it: the capability count on
    the Expert card, the checked boxes in the editor, and the "use a skill
    temporarily" menu, which can only offer what the Expert does not already
    have. The declaration now reads as the concrete set this package ships, so
    each entry has to be a capability that exists in this checkout.

    Selecting a Plugin already brings its MCP servers, Skills and Tools, so the
    starter does not name them again: `mcps` is empty because the servers it
    uses arrive with the Plugins it selects.
    """
    import yaml

    from bianbu_agent.tools.builtins import BUILTIN_MODULES

    agent = yaml.safe_load(
        (PROJECT_ROOT / "resources" / "default-profile" / "starter-agent.yaml").read_text(
            encoding="utf-8"
        )
    )["agent"]
    declared_skills = set(agent["skills"])
    shipped_skills = {
        path.parent.name
        for path in (PROJECT_ROOT / "resources" / "skills").glob("*/SKILL.md")
    }
    shipped_plugins = {
        path.name
        for path in (PROJECT_ROOT / "resources" / "plugins").iterdir()
        if path.is_dir()
    }
    shipped_mcps: set[str] = set()
    for plugin in (PROJECT_ROOT / "resources" / "plugins").iterdir():
        if not (plugin / "mcps").is_dir():
            continue
        shipped_mcps.update(f"{plugin.name}/{server.name}" for server in (plugin / "mcps").iterdir())

    for group, known in (
        ("tools", set(BUILTIN_MODULES)),
        ("skills", shipped_skills),
        ("mcps", shipped_mcps),
        ("plugins", shipped_plugins),
    ):
        selectors = agent[group]
        assert "*" not in selectors, f"starter Agent {group} must name ids, not a wildcard"
        if group != "mcps":
            assert selectors, f"starter Agent {group} must not be empty"
        unknown = sorted(set(selectors) - known)
        assert not unknown, f"starter Agent {group} declares capabilities this checkout lacks: {unknown}"

    selected_plugins = set(agent["plugins"])
    assert selected_plugins
    for group in ("mcps", "skills", "tools"):
        for selector in agent[group]:
            owner = selector.split("/", 1)[0] if "/" in selector else selector
            assert owner not in selected_plugins, (
                f"starter Agent {group} names {selector}, which the selected "
                f"Plugin {owner} already brings"
            )

    # The preset skills are fixed on every Expert; the starter still names them
    # so its own declaration is complete on its own.
    assert "using-apm" in declared_skills


@pytest.mark.asyncio
async def test_packaged_expert_materializes_the_mcps_its_plugins_own() -> None:
    """`mcps: []` narrows the declaration, not the capability.

    Selecting `bianbu.bibit.intent` and `bianbu.memory` is what brings their
    MCP servers into the session, so the packaged Expert still resolves to the
    four servers even though it no longer names them. The catalog here is the
    one this checkout ships: every server under `resources/plugins/*/mcps/`,
    owned by the Plugin that carries it, plus one unowned server that the
    empty declaration has to leave out.
    """

    spec = AgentManifest.from_path(
        PROJECT_ROOT / "resources" / "default-profile" / "starter-agent.yaml"
    ).spec
    shipped = {
        f"{plugin.name}/{server.name}": SimpleNamespace(owner_plugin_id=plugin.name)
        for plugin in (PROJECT_ROOT / "resources" / "plugins").iterdir()
        if (plugin / "mcps").is_dir()
        for server in (plugin / "mcps").iterdir()
    }
    assert set(shipped) == {
        "bianbu.bibit.intent/bibit-admin",
        "bianbu.bibit.intent/bibit-control",
        "bianbu.bibit.intent/bibit-readonly",
        "bianbu.memory/memory",
    }
    generation = SimpleNamespace(
        plugins={plugin: object() for plugin in spec.plugins},
        tools={
            name: SimpleNamespace(plugin_id=None, spec=SimpleNamespace(metadata={}))
            for name in spec.tools
        },
        skills={
            name: SimpleNamespace(plugin_id=None, spec=SimpleNamespace(metadata={}))
            for name in spec.skills
        },
        hooks={},
        mcps={**shipped, "mcp.elsewhere/server": SimpleNamespace(owner_plugin_id=None)},
        mcp_tools={},
    )
    manager = AgentManager(
        store=InMemoryAgentRegistry((spec,)),
        model_registry=SimpleNamespace(resolve=lambda model: model),
        generation_provider=lambda: generation,
    )

    materialized = await manager.materialize(spec.id, generation)

    assert set(materialized.mcps) == set(shipped)
    assert set(materialized.plugins) == set(spec.plugins)


def test_tracked_plugins_and_hooks_use_convention_only_definitions() -> None:
    plugins = PROJECT_ROOT / "resources" / "plugins"
    assert {path.name for path in plugins.iterdir() if path.is_dir()} == {
        "bianbu.audit",
        "bianbu.bibit.intent",
        "bianbu.memory",
        "bianbu.security",
    }
    for plugin in (path for path in plugins.iterdir() if path.is_dir()):
        assert not (plugin / "plugin.toml").exists()
        hooks = plugin / "hooks"
        if hooks.is_dir():
            for hook in hooks.iterdir():
                    assert {
                        path.name for path in hook.iterdir() if path.name != "__pycache__"
                    } == {"hook.json"}


def test_tracked_project_home_contains_valid_actual_capability_definitions(tmp_path, monkeypatch) -> None:
    home = AgentHome(tmp_path / "user").prepare()
    monkeypatch.setenv("BIANBU_SYSTEM_RESOURCE_ROOT", str(PROJECT_ROOT / "resources"))

    skills = [path for path in (PROJECT_ROOT / "resources/skills").iterdir() if path.is_dir()]
    for root in skills:
        DirectorySkillLoader.inspect(root)

    plugins = PluginInstaller(home).list()
    from bianbu_agent.tools.builtins import BUILTIN_MODULES, inspect_builtin
    assert len(BUILTIN_MODULES) == 34
    for name in BUILTIN_MODULES:
        assert inspect_builtin(name).source_path.is_file()
    # 计数是刻意写死的绊线：新增/删除随包技能时必须显式改这里。
    # 9 = 9 个随包技能。plugin-creator 已于 2026-09-21 删除：插件目录与清单由
    # APM/插件管理界面和 Codex 侧的 plugin-creator 覆盖，Runtime 不再随包一个
    # 本地脚手架 Skill。第三方 Power（spacemit-power、superpowers）自 2026-09-29
    # 起不再随包，用户按需从 AgentHub 装到 ~/.bianbu-agent/plugins/。
    assert len(skills) == 9
    assert len(SkillPackageManager(home).list()) == len(skills)
    assert all(SkillPackageManager(home).verify(path.name) for path in skills)
    assert {plugin.id for plugin in plugins} == {
        "bianbu.audit",
        "bianbu.bibit.intent",
        "bianbu.memory",
        "bianbu.security",
    }
    qualified = {member.qualified_id for member in PluginCatalog(home).list()}
    assert {
        "bianbu.memory/memory.before_run",
        "bianbu.memory/memory.after_run",
        "bianbu.security/security.before_model",
        "bianbu.security/security.before_tool",
        "bianbu.audit/audit.after_run",
        "bianbu.audit/audit.after_tool",
        "bianbu.audit/audit.on_error",
        "bianbu.bibit.intent/bibit-intent.before_run",
        "bianbu.memory/memory",
    } <= qualified
    # Bibit Intent 的 MCP 已拆成只读/控制/管理三个 server，旧的单入口退场。
    assert {
        "bianbu.bibit.intent/bibit-readonly",
        "bianbu.bibit.intent/bibit-control",
        "bianbu.bibit.intent/bibit-admin",
    } <= qualified
    assert "bianbu.bibit.intent/bibit-intent" not in qualified


def test_tracked_starter_agent_template_is_valid() -> None:
    manifest = AgentManifest.from_path(PROJECT_ROOT / "resources" / "default-profile" / "starter-agent.yaml")

    assert manifest.spec.id == "starter-template"
    assert manifest.spec.name == "BibitEdgeAssistant"
    assert manifest.spec.name_zh == "Bibit端侧智能助手"
    # 默认专家点名它随包发布的每一项能力，而不是交给通配符展开。
    assert all("*" not in selectors for selectors in (
        manifest.spec.tools,
        manifest.spec.skills,
        manifest.spec.mcps,
        manifest.spec.plugins,
    ))
    assert "using-apm" in manifest.spec.skills
    assert "read_file" in manifest.spec.tools
    assert manifest.spec.metadata.get("avatar") == "🤖"


def test_memory_security_and_audit_have_no_top_level_home_directories() -> None:
    home = AgentHome(PROJECT_ROOT / ".bianbu-agent")

    assert not (home.root / "memory").exists()
    assert not (home.root / "security").exists()
    assert not (home.root / "audit").exists()


@pytest.mark.asyncio
async def test_bundled_hooks_run_from_installed_plugin_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIANBU_AGENT_MEMORY_ENABLED", "false")
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    shutil.copytree(
        PROJECT_ROOT / "resources" / "plugins",
        home.plugins_dir,
        dirs_exist_ok=True,
    )

    class Model:
        model = "test"

    class Engine:
        async def stream(self, prompt, **kwargs):
            yield RunEvent("done", {})

    default = AgentSpec(id="default", name="Default", plugins=("*",))
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(
            data_dir=home.root,
            plugin_dirs=(home.plugins_dir,),
            skill_dirs=(home.skills_dir,),
        ),
        default_model=Model(),
        agent_id=default.id,
        agent_registry=InMemoryAgentRegistry((default,)),
        engine_builder=lambda *args: Engine(),
    )

    async with sdk:
        generation = sdk.plugin_runtime.current_generation
        assert set(generation.plugins) == {
            "bianbu.audit",
            "bianbu.bibit.intent",
            "bianbu.memory",
            "bianbu.security",
        }
        common = {
            "run_id": "run-1",
            "session_id": "session-1",
            "agent_id": "default",
            "request": "hello",
        }
        await generation.run_hooks(
            HookPoint.AFTER_RUN,
            RunContext(**common, metadata={"memory_to_record": "remember me"}),
        )
        await generation.run_hooks(
            HookPoint.AFTER_TOOL,
            RunContext(**common, metadata={"tool": "memory_record"}),
        )
        with pytest.raises(PermissionError, match="denied by bianbu.security"):
            await generation.run_hooks(
                HookPoint.BEFORE_TOOL,
                RunContext(**common, metadata={"security_denied": True}),
            )

    data_root = home.workspace_dir / "plugin-data"
    assert "remember me" in (
        data_root / "bianbu.memory" / "data" / "memory.jsonl"
    ).read_text(encoding="utf-8")
    assert "tool.completed" in (
        data_root / "bianbu.audit" / "data" / "audit.jsonl"
    ).read_text(encoding="utf-8")
    assert not (home.plugins_dir / "bianbu.memory" / "data").exists()
