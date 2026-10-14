from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import pytest

from bianbu_agent import (
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    RunEvent,
    RuntimeConfig,
)
from bianbu_agent.home import AgentHome
from bianbu_agent.plugins import ContextPatch, HookPoint, PluginInstaller, RunContext


@dataclass
class Model:
    model: str


class Engine:
    async def stream(self, prompt, **kwargs):
        yield RunEvent("token", {"text": "ok"})
        yield RunEvent("done", {})


def _plugin(root: Path, plugin_id: str) -> None:
    root.mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        f"""\
schema_version: 1
name: {plugin_id}
version: 1.0.0
description: Package runtime fixture.
""",
        encoding="utf-8",
    )
    tool = root / "tools" / "echo"
    tool.mkdir(parents=True)
    (tool / "tool.py").write_text(
        dedent(
            """\
            from bianbu_agent.tools import tool

            def create_tool(context):
                @tool
                def echo() -> str:
                    \"\"\"Return the Tool name.\"\"\"
                    return context.name
                return echo
            """
        ),
        encoding="utf-8",
    )
    hook = root / "hooks" / "lifecycle.before_run"
    hook.mkdir(parents=True)
    (hook / "hook.json").write_text(json.dumps({"schema_version": 1, "hooks": {"BeforeRun": [{"hooks": [{"type": "command", "command": 'exec "${RUNTIME_PYTHON}" "${PLUGIN_ROOT}/scripts/before_run.py"'}]}]}}))
    (root / "scripts").mkdir()
    (root / "scripts" / "before_run.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "if request['metadata'].get('deny') is True:\n"
        "    result = {'decision': 'deny', 'reason': 'denied by package hook'}\n"
        "else:\n"
        "    data = Path(os.environ['PLUGIN_DATA_ROOT'])\n"
        "    data.mkdir(parents=True, exist_ok=True)\n"
        "    (data / 'hook-ran').write_text(request['run_id'])\n"
        f"    result = {{'metadata': {{'owner': {plugin_id!r}}}}}\n"
        "json.dump(result, sys.stdout)\n"
    )



@pytest.mark.asyncio
async def test_installed_plugin_owns_versioned_namespaced_runtime_contributions(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    plugin_id = "vendor.first"
    source = tmp_path / plugin_id
    _plugin(source, plugin_id)
    PluginInstaller(home).install(source)

    default = AgentSpec(
        id="default",
        name="Default",
        plugins=("vendor.first",),
    )
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(
            data_dir=home.root,
            plugin_dirs=(home.plugins_dir,),
            skill_dirs=(home.skills_dir,),
        ),
        default_model=Model("default"),
        agent_id=default.id,
        agent_registry=InMemoryAgentRegistry((default,)),
        engine_builder=lambda *args: Engine(),
    )

    async with sdk:
        generation = sdk.plugin_runtime.current_generation
        from bianbu_agent.tools.builtins import BUILTIN_MODULES
        assert set(generation.tools) == set(BUILTIN_MODULES) | {"vendor.first/echo"}
        assert generation.plugins[plugin_id].instance.manifest.version == "1.0.0"
        patch = await generation.run_hooks(
            HookPoint.BEFORE_RUN,
            RunContext(
                run_id="runtime-proof",
                session_id="session",
                agent_id="default",
                request="hello",
            ),
        )
        assert isinstance(patch, ContextPatch)
        assert patch.metadata == {"vendor.first": {"owner": "vendor.first"}}
        with pytest.raises(PermissionError, match="denied by package hook"):
            await generation.run_hooks(
                HookPoint.BEFORE_RUN,
                RunContext(
                    run_id="denied",
                    session_id="session",
                    agent_id="default",
                    request="blocked",
                    metadata={"deny": True},
                ),
            )

    install_root = home.plugins_dir / plugin_id
    assert not (install_root / "data").exists()
    assert (install_root / "tools" / "echo" / "tool.py").is_file()
    assert not (home.root / "tools").exists()
    marker = home.workspace_dir / "plugin-data" / plugin_id / "data" / "hook-ran"
    assert marker.read_text(encoding="utf-8") == "runtime-proof"


@pytest.mark.asyncio
async def test_directory_tool_and_command_hook_receive_same_config_snapshot(tmp_path):
    from bianbu_agent.plugins.directory import DirectoryPackagePlugin
    from bianbu_agent.plugins.manifest import load_plugin_manifest
    from bianbu_agent.plugins.composition import compose_plugin_config
    from bianbu_agent.plugins.models import PluginContext
    source = tmp_path / 'vendor.first'
    _plugin(source, 'vendor.first')
    (source / 'config.yaml').write_text('nested:\n  limit: 9\n')
    tool_path = source / 'tools/echo/tool.py'
    tool_path.write_text(tool_path.read_text().replace(
        'return context.name', 'return str(context.config["nested"]["limit"])').replace(
        'def create_tool(context):', 'def create_tool(context):\n    assert context.config["nested"]["limit"] == 12'))
    (source / 'scripts/before_run.py').write_text(
        'import json,sys\np=json.load(sys.stdin)\nassert p["config"] == {"nested":{"limit":12}}\nprint(json.dumps({"metadata":{"config":p["config"]}}))\n')
    manifest = load_plugin_manifest(source)
    context = PluginContext(plugin_id=manifest.id, data_dir=tmp_path / 'data',
        config=compose_plugin_config(manifest, runtime_config={'nested': {'limit': 12}}).config)
    context.data_dir.mkdir()
    plugin = DirectoryPackagePlugin(manifest, root=source, workspace=tmp_path)
    try:
        await plugin.setup(context)
        hook = context.scope.snapshot().hooks[0]
        patch = await hook.handler(RunContext('r', 's', 'a', 'hello'))
        assert patch.metadata == {'config': {'nested': {'limit': 12}}}
    finally:
        await plugin.stop()
