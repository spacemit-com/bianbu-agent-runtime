"""K04/K08：`k3-smart-office` 技能的契约。

模板在 bibit 侧（`template_list` / `template_get`），「这类办公任务怎么做」在 Runtime
侧。这个技能必须：

- 经 Runtime 的真实加载器可加载（Power 格式 v2 的登记键、单行 frontmatter、目录名与 name 一致）；
- 先取模板再执行计划，而不是让模型自己编结构；
- 先出草稿（用 fallback、最多问 2 个问题），不许用编造的事实填空；
- 内容先本地后云端，来源写进文档的「资料来源」；
- 以产物为准，禁止直接跑二进制 / 进 REPL / 自己装依赖。
"""
from __future__ import annotations

from pathlib import Path

from bianbu_agent.skills.loader import DirectorySkillLoader

SKILL_ROOT = Path(__file__).resolve().parents[3] / "resources" / "skills" / "k3-smart-office"


def _instructions() -> str:
    """用 Runtime 的真实加载器取正文（inspect 只返回元数据与文件清单）。"""
    return DirectorySkillLoader().load(SKILL_ROOT).spec.instructions


def test_skill_loads_with_the_real_loader() -> None:
    metadata, files = DirectorySkillLoader.inspect(SKILL_ROOT)

    assert metadata["name"] == "k3-smart-office"
    assert metadata["version"] == "1.0.0"
    assert metadata["description"].strip()
    assert "\n" not in metadata["description"]
    assert [path.name for path in files] == ["SKILL.md"]
    assert _instructions().strip()


def test_skill_name_matches_its_directory() -> None:
    metadata, _ = DirectorySkillLoader.inspect(SKILL_ROOT)

    assert metadata["name"] == SKILL_ROOT.name


def test_skills_list_and_skill_view_reach_it() -> None:
    """`skills_list` 列得出、`skill_view` 打得开。

    内置技能不再写 display_name：列表里显示的就是这个目录名（英文、小写），
    加载器以 name 兜底，所以这里固定的是兜底结果而不是一条自造的中文标题。
    """
    metadata, _ = DirectorySkillLoader.inspect(SKILL_ROOT)

    assert "display_name" not in metadata
    assert DirectorySkillLoader().load(SKILL_ROOT).spec.display_name == "k3-smart-office"


def test_template_catalog_is_used_before_executing() -> None:
    instructions = _instructions()

    for tool in ("template_list", "template_get", "appcli_run", "appcli_verify", "file_search"):
        assert tool in instructions, f"办公技能必须提到 {tool}"
    for template in (
        "weekly_report",
        "meeting_minutes",
        "project_plan",
        "data_report",
        "presentation",
    ):
        assert template in instructions, f"内置模板 {template} 必须能被模型认出来"
    workflow = instructions[instructions.index("## 1.") : instructions.index("## 4.")]
    assert workflow.index("template_list") < workflow.index("template_get")
    assert workflow.index("template_get") < instructions.index("## 4.")


def test_skill_asks_the_model_to_draft_first() -> None:
    instructions = _instructions()

    assert "fallback" in instructions
    assert "at most two" in instructions.lower()
    assert "待补充" in instructions or "TODO" in instructions
    assert "Never invent" in instructions


def test_skill_grounds_content_and_records_sources() -> None:
    instructions = _instructions()
    grounding = instructions[instructions.index("## 3.") : instructions.index("## 4.")]

    assert "file_search" in grounding
    assert "read the file before quoting it" in grounding
    assert "web_search" in grounding
    assert "资料来源" in grounding or "Sources" in grounding


def test_skill_warns_about_unfillable_writer_tables() -> None:
    """真实会话教训：模型建了空表，然后翻了十几个工具调用去找填格命令。"""
    instructions = _instructions()

    assert "writer_add_table" in instructions
    assert "empty" in instructions.lower()
    assert "Calc templates" in instructions or "calc" in instructions.lower()


def test_skill_refuses_to_bypass_the_tool_layer() -> None:
    instructions = _instructions()
    lowered = instructions.lower()

    assert "repl" in lowered and "never" in lowered
    assert "pip install" in lowered and "apt install" in lowered
    assert "exit code is not a result" in lowered
    assert "needs_host_channel" in instructions
    assert "dep_plan" in instructions and "dep_apply" in instructions
