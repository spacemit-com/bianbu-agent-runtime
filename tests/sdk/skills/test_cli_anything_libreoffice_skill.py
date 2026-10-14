"""H12：转换出的 per-app 技能（`cli-anything-libreoffice`）的契约。

产物由 bibit-intent 的构建期转换器生成，**不要手改**——上游模板一改，手改的副本会
静默失效。重新生成：

    CLI_ANYTHING_UPSTREAM=~/workspace/CLI-Anything \
    python scripts/convert_appcli_skills.py --upstream "$CLI_ANYTHING_UPSTREAM" \
        --out <runtime>/resources/skills

上游技能是「这个 CLI 怎么用」，Runtime 技能是「这类任务怎么做」，两者格式不兼容
（上游的块标量 frontmatter、`author`/`requires` 等键会被 Runtime 加载器硬拒）。这里
固定转换后必须成立的三件事：真实加载器能读、目录名与 frontmatter 名一致、上游的
「直接 pip 安装」和「进 REPL」两种语义没有漏进来。
"""
from __future__ import annotations

from pathlib import Path

from bianbu_agent.skills.loader import DirectorySkillLoader

SKILL_ROOT = (
    Path(__file__).resolve().parents[3] / "resources" / "skills" / "cli-anything-libreoffice"
)


def _instructions() -> str:
    """用 Runtime 的真实加载器取正文（inspect 只返回元数据与文件清单）。"""
    return DirectorySkillLoader().load(SKILL_ROOT).spec.instructions


def test_converted_skill_loads_with_the_real_loader() -> None:
    metadata, files = DirectorySkillLoader.inspect(SKILL_ROOT)

    assert metadata["name"] == "cli-anything-libreoffice"
    assert metadata["version"] == "1.0.0"
    assert metadata["description"].strip()
    # 上游用 `description: >-` 折叠块标量，Runtime 的行式解析器会把续行当 frontmatter 行而拒绝。
    assert "\n" not in metadata["description"]
    assert [path.name for path in files] == ["SKILL.md"]
    assert _instructions().strip()


def test_skill_name_matches_its_directory() -> None:
    """`skills_list` 读 frontmatter 名、`skill_view` 读目录名，不一致就会「列得出、打不开」。"""
    metadata, _ = DirectorySkillLoader.inspect(SKILL_ROOT)

    assert metadata["name"] == SKILL_ROOT.name


def test_converted_skill_keeps_the_appcli_run_constraint() -> None:
    instructions = _instructions()

    for tool in ("appcli_list", "appcli_run", "appcli_verify"):
        assert tool in instructions, f"转换后的技能必须提到 {tool}"
    assert "repl" in instructions.lower(), "必须明说不进交互式 REPL"


def test_upstream_install_and_repl_semantics_are_gone() -> None:
    """上游正文教模型 `pip install git+github` 和进 REPL，两者都与本仓策略冲突。"""
    instructions = _instructions()

    assert "## Installation" not in instructions
    assert "## REPL Mode" not in instructions
    assert "pip install" not in instructions
    assert "git+" not in instructions
