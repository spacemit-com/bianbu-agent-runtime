from pathlib import Path
from types import SimpleNamespace
import pytest
from bianbu_agent.agents import AgentModel, AgentSpec, AgentManifest, InMemoryAgentRegistry
from bianbu_agent.agents.manager import AgentManager


def member(owner):
    return SimpleNamespace(plugin_id=owner, spec=SimpleNamespace(metadata={}))


@pytest.mark.asyncio
async def test_plugin_selection_materializes_members_as_one_unit():
    generation = SimpleNamespace(plugins={'example.bundle': object()},
        tools={'bundle_tool': member('example.bundle'), 'loose_tool': member('directory.tools')},
        skills={'bundle_skill': member('example.bundle'), 'loose_skill': member('directory.skills')},
        hooks={'bundle_hook': member('example.bundle'), 'loose_hook': member('directory.hooks')},
        mcps={'bundle_mcp': SimpleNamespace(owner_plugin_id='example.bundle'),
              'loose_mcp': SimpleNamespace(owner_plugin_id=None)}, mcp_tools={'bundle_mcp': ('bundle_remote',)})
    registry = InMemoryAgentRegistry()
    manager = AgentManager(store=registry, model_registry=SimpleNamespace(resolve=lambda model: model),
                           generation_provider=lambda: generation)
    selected = AgentSpec(id='chosen', name='Chosen', model=AgentModel('test', 'test'), plugins=('example.bundle',))
    await manager.create(selected)
    actual = await manager.materialize('chosen', generation)
    assert actual.tools == ('bundle_tool', 'bundle_remote')
    assert actual.skills == ('bundle_skill',)
    assert actual.hooks == ('bundle_hook',)
    assert actual.mcps == ('bundle_mcp',)
    await manager.create(AgentSpec(id='loose', name='Loose', model=AgentModel('test', 'test'), tools=('*',), skills=('*',), mcps=('*',)))
    independent = await manager.materialize('loose', generation)
    assert independent.tools == ('loose_tool',)
    assert independent.skills == ('loose_skill',)
    assert independent.hooks == ()
    assert independent.mcps == ('loose_mcp',)


def test_plugin_selection_round_trips_manifest():
    spec = AgentSpec(id='example', name='Example', plugins=('*',))
    manifest = AgentManifest.from_yaml('schema_version: 1\nagent:\n  id: example\n  name: Example\n  version: 1.0.0\n  plugins: ["*"]\n')
    assert manifest.spec.plugins == spec.plugins
    assert AgentManifest.from_yaml(manifest.to_yaml()).spec.plugins == ('*',)


@pytest.mark.asyncio
async def test_intent_ownership_move_preserves_existing_security_grant(monkeypatch):
    from bianbu_agent.server import runtime
    calls = []
    def check(agent, capability):
        calls.append((agent, capability))
        return 0
    monkeypatch.setattr(runtime, 'security_client_module', lambda: SimpleNamespace(
        get_agent_security_mode=lambda _: {'mode':'normal'}, check_permission=check))
    assert await runtime._securityd_allows_mcp('expert', 'bianbu.bibit.intent/bibit-intent')
    assert calls == [('expert', 'mcp.bibit-intent')]
    for mcp_id in (
        'bianbu.bibit.intent/bibit-readonly',
        'bianbu.bibit.intent/bibit-control',
        'bianbu.bibit.intent/bibit-admin',
    ):
        assert await runtime._securityd_allows_mcp('expert', mcp_id)
    assert calls == [
        ('expert', 'mcp.bibit-intent'),
        ('expert', 'mcp.bibit-intent'),
        ('expert', 'mcp.bibit-intent'),
        ('expert', 'mcp.bibit-intent'),
    ]


@pytest.mark.asyncio
async def test_manifest_without_plugin_selection_keeps_plugin_members_out():
    manifest = AgentManifest.from_yaml(
        'schema_version: 1\nagent:\n  id: old\n  name: Old\n  version: 1.0.0\n  model:\n    provider: test\n    name: test\n'
    )
    generation = SimpleNamespace(plugins={'example.bundle':object()},
        tools={'bundle_tool':member('example.bundle')}, skills={},
        hooks={'bundle_hook':member('example.bundle')},
        mcps={'server':SimpleNamespace(owner_plugin_id='example.bundle')}, mcp_tools={})
    registry = InMemoryAgentRegistry([manifest.spec])
    manager = AgentManager(store=registry, model_registry=SimpleNamespace(resolve=lambda model: model), generation_provider=lambda:generation)
    actual = await manager.materialize('old', generation)
    assert actual.hooks == ()
    assert actual.tools == actual.mcps == ()
    assert actual.plugins == ()
    assert AgentManifest.from_yaml(manifest.to_yaml()).spec.plugins == ()
    from dataclasses import replace
    await registry.update(replace(manifest.spec, plugins=()))
    actual = await manager.materialize('old', generation)
    assert actual.hooks == ()
    assert actual.tools == ()


def test_packaged_expert_does_not_name_members_of_its_own_plugins():
    """A selected Plugin already brings its MCP servers, Skills and Tools.

    Naming them a second time in `mcps:` (or `skills:`/`tools:`) is the same
    declaration twice, which is how the packaged Expert ended up listing the
    Bibit Intent and memory servers while selecting the Plugins that own them.
    """

    spec = AgentManifest.from_path(
        Path(__file__).parents[2] / "resources" / "default-profile" / "starter-agent.yaml"
    ).spec
    selected_plugins = set(spec.plugins)
    assert selected_plugins and "*" not in selected_plugins
    for selector in (*spec.mcps, *spec.skills, *spec.tools):
        owner = selector.split("/", 1)[0] if "/" in selector else selector
        assert owner not in selected_plugins, (
            f"{selector} belongs to the selected Plugin {owner}: "
            "the Plugin selection already brings it"
        )


def test_manifest_rejects_agent_level_hooks():
    with pytest.raises(ValueError, match='unsupported agent fields'):
        AgentManifest.from_yaml('schema_version: 1\nagent:\n  id: old\n  name: Old\n  version: 1.0.0\n  hooks: [bundle_hook]\n')


def test_manifest_rejects_an_empty_hooks_list_too():
    with pytest.raises(ValueError, match='unsupported agent fields'):
        AgentManifest.from_yaml('schema_version: 1\nagent:\n  id: old\n  name: Old\n  version: 1.0.0\n  hooks: []\n')
