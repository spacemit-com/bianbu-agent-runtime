from __future__ import annotations

import pytest

from bianbu_agent.plugins import (
    ContributionConflictError,
    PluginContributions,
    PluginGeneration,
    PluginRuntime,
    SkillContribution,
    SkillSpec,
    ToolContribution,
    ToolResult,
    ToolSpec,
)


async def _handler(_context, _arguments):
    return ToolResult("ok")


def _runtime(plugin_id: str, *, tool: str = "", skill: str = "") -> PluginRuntime:
    return PluginRuntime(
        plugin_id=plugin_id,
        instance=object(),
        config={},
        contributions=PluginContributions(
            tools=(
                ToolContribution(plugin_id, ToolSpec(tool, "", {"type": "object"}), _handler),
            )
            if tool
            else (),
            skills=(SkillContribution(plugin_id, SkillSpec(skill)),) if skill else (),
        ),
    )


def test_generation_builds_immutable_tool_and_skill_catalogs() -> None:
    generation = PluginGeneration(
        id=1,
        plugins={
            "z.plugin": _runtime("z.plugin", tool="z_tool"),
            "a.plugin": _runtime("a.plugin", skill="a-skill"),
        },
        statuses={},
    )

    assert tuple(generation.tools) == ("z_tool",)
    assert tuple(generation.skills) == ("a-skill",)
    with pytest.raises(TypeError):
        generation.tools["new"] = generation.tools["z_tool"]


@pytest.mark.parametrize("kind", ["tool", "skill"])
def test_generation_rejects_duplicate_tool_and_skill_names(kind: str) -> None:
    kwargs = {kind: "same_name"}
    with pytest.raises(ContributionConflictError, match="same_name"):
        PluginGeneration(
            id=1,
            plugins={
                "a.plugin": _runtime("a.plugin", **kwargs),
                "b.plugin": _runtime("b.plugin", **kwargs),
            },
            statuses={},
        )


def test_generation_rejects_spoofed_contribution_owner() -> None:
    runtime = _runtime("actual.plugin", tool="safe_tool")
    spoofed = PluginRuntime(
        plugin_id="actual.plugin",
        instance=object(),
        config={},
        contributions=PluginContributions(
            tools=(
                ToolContribution(
                    "other.plugin",
                    runtime.contributions.tools[0].spec,
                    _handler,
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="owner"):
        PluginGeneration(id=1, plugins={"actual.plugin": spoofed}, statuses={})

