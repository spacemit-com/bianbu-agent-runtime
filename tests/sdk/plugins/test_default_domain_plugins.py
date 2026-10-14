from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bianbu_agent import (
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    RunEvent,
    RuntimeConfig,
)
from bianbu_agent.home import AgentHome


PROJECT_ROOT = Path(__file__).parents[3]


class _Model:
    model = "test"


class _Engine:
    async def stream(self, prompt, **kwargs):
        yield RunEvent("done", {})


@pytest.mark.asyncio
async def test_default_domain_plugins_capture_members_without_bibit_console(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    shutil.copytree(
        PROJECT_ROOT / "resources/plugins",
        home.plugins_dir,
        dirs_exist_ok=True,
    )
    default = AgentSpec(id="default", name="Default", plugins=("*",), tools=("*",), skills=("*",))
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(
            data_dir=home.root,
            plugin_dirs=(home.plugins_dir,),
            skill_dirs=(home.skills_dir,),
        ),
        default_model=_Model(),
        agent_id=default.id,
        agent_registry=InMemoryAgentRegistry((default,)),
        engine_builder=lambda *args: _Engine(),
    )

    async with sdk:
        generation = sdk.plugin_runtime.current_generation
        assert set(generation.plugins) == {
            "bianbu.memory",
            "bianbu.security",
            "bianbu.audit",
            "bianbu.bibit.intent",
        }
        # Memory Tools now arrive through the plugin's MCP server, so the
        # generation carries an MCP runtime instead of four directory Tools.
        assert [
            name for name in generation.tools if name.startswith("bianbu.memory")
        ] == []
        assert {"memory", "memory_record", "memory_retrieve"}.isdisjoint(
            generation.tools
        )
        assert {
            "bianbu.memory/memory-guide",
        } <= set(generation.skills)
        assert "bianbu.bibit.intent/bibit-intent" not in generation.skills
        assert "bianbu.bibit.intent/intent_classify" not in generation.tools
        # Bianbu Agent no longer hosts plugin Consoles: the Bibit model page
        # reads the AI Gateway through the read-only local model resource.
        assert "bianbu.bibit.intent" not in {
            view.owner_plugin_id for view in generation.mcps.values()
        }
