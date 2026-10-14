"""H12：`appcli-router` 技能的契约。

CLI-Anything 的上游技能是「某个 CLI 怎么用」，Runtime 技能是「这类任务怎么做」。
路由技能是本仓唯一手写的 appcli 技能，它必须：
- 经 Runtime 的真实加载器可加载（Power 格式 v2 的登记键、单行 frontmatter）；
- 教模型先 `appcli_list` 再调用、缺前置走依赖计划、以产物为准；
- 明确禁止直接跑二进制、进 REPL、绕过 riscv64 验收。
"""
from __future__ import annotations

from pathlib import Path

from bianbu_agent.skills.loader import DirectorySkillLoader

SKILL_ROOT = Path(__file__).resolve().parents[3] / "resources" / "skills" / "appcli-router"


def _instructions() -> str:
    """用 Runtime 的真实加载器取正文（inspect 只返回元数据与文件清单）。"""
    return DirectorySkillLoader().load(SKILL_ROOT).spec.instructions


def test_router_skill_loads_with_the_real_loader() -> None:
    metadata, files = DirectorySkillLoader.inspect(SKILL_ROOT)

    assert metadata["name"] == "appcli-router"
    assert metadata["version"] == "1.0.0"
    assert metadata["description"].strip()
    assert "\n" not in metadata["description"]
    assert [path.name for path in files] == ["SKILL.md"]
    assert _instructions().strip()


def test_router_skill_points_at_the_appcli_tools() -> None:
    instructions = _instructions()

    for tool in ("appcli_list", "appcli_run", "appcli_verify"):
        assert tool in instructions, f"路由技能必须提到 {tool}"
    assert "dep_plan" in instructions and "dep_apply" in instructions
    # 工作流里必须「先发现再调用」，而不是让模型猜 appcli/action
    workflow = instructions[instructions.index("## Workflow") : instructions.index("## Boundaries")]
    assert workflow.index("appcli_list") < workflow.index("appcli_run")


def test_router_skill_forbids_bypassing_the_tool_layer() -> None:
    instructions = _instructions()
    lowered = instructions.lower()

    assert "repl" in lowered and "never" in lowered
    assert "pip install" in lowered, "必须点名禁止直接 pip 安装"
    assert "exit code" in lowered, "必须说明 exit 0 不等于成功"
    assert "needs_host_channel" in instructions
    assert "riscv64" in lowered


def test_router_skill_separates_the_two_paths() -> None:
    instructions = _instructions()

    assert "application-launch path" in instructions
    assert "drawio" in instructions, "已有 .drawio 文件与从零造图必须分开写清"
