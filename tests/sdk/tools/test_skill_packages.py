from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bianbu_agent.tools.builtins import load_builtin

from bianbu_agent import ToolCallContext
from bianbu_agent.tools import (
    DirectoryToolLoader,
    ToolLoadContext,
    ToolRuntimeFactory,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_skill_tools_read_task_snapshot_after_live_files_are_removed(tmp_path):
    from bianbu_agent.skills import DirectorySkillLoader
    from bianbu_agent.skills.versions import task_skills

    root = _sandbox(tmp_path)
    snapshot = tmp_path / "pinned" / "example-skill"
    shutil.copytree(root / "skills" / "example-skill", snapshot)
    skill = DirectorySkillLoader().load(snapshot)
    token = task_skills.set({"example-skill": skill})
    call = ToolCallContext("run", "session", "agent")
    try:
        viewed = _load(root, "skill_view", tmp_path / "workspace")
        listed = _load(root, "skills_list", tmp_path / "workspace")
        shutil.rmtree(root / "skills")
        result = json.loads((await viewed.handler(call, {
            "name": "example-skill", "file_path": "references/note.md",
        })).content)
        assert result["content"] == "Reference."
        assert result["skill_root"] == str(snapshot)
        assert json.loads((await listed.handler(call, {})).content)["total_count"] == 1
        empty_token = task_skills.set({})
        try:
            assert json.loads((await viewed.handler(call, {"name": "example-skill"})).content)["code"] == "not_found"
            assert json.loads((await listed.handler(call, {})).content)["total_count"] == 0
        finally:
            task_skills.reset(empty_token)
    finally:
        task_skills.reset(token)


@pytest.fixture(autouse=True)
def isolated_skill_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BIANBU_USER_SKILLS_DIR", raising=False)


def _sandbox(tmp_path: Path) -> Path:
    root = tmp_path / ".bianbu-agent"
    skill = root / "skills" / "example-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: example-skill\ndescription: Example description\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (skill / "references").mkdir()
    (skill / "references" / "note.md").write_text("Reference.", encoding="utf-8")
    return root


def _load(root: Path, name: str, workspace: Path):
    return load_builtin(
        name,
        context=ToolLoadContext(
            workspace_root=workspace,
            services={"runtime_factory": ToolRuntimeFactory(workspace_root=workspace), "home_root": root},
        ),
    )


@pytest.mark.asyncio
async def test_skill_list_and_view_are_directory_owned(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    workspace = tmp_path / "workspace"
    call = ToolCallContext("run", "session", "agent")

    listed = await _load(root, "skills_list", workspace).handler(call, {})
    viewed = await _load(root, "skill_view", workspace).handler(
        call, {"name": "example-skill"}
    )

    assert json.loads(listed.content)["skills"] == [
        {"name": "example-skill", "description": "Example description"}
    ]
    viewed_payload = json.loads(viewed.content)
    assert "Body." in viewed_payload["content"]
    assert viewed_payload["skill_root"] == str(root / "skills/example-skill")
    assert viewed_payload["absolute_path"] == str(root / "skills/example-skill/SKILL.md")
    assert viewed_payload["linked_files"] == {
        "references": ["references/note.md"],
        "templates": [],
        "scripts": [],
        "assets": [],
    }


def test_skill_manage_schema_has_no_ignored_absorption_parameter(
    tmp_path: Path,
) -> None:
    root = _sandbox(tmp_path)
    tool = _load(root, "skill_manage", tmp_path / "workspace")

    assert "absorbed_into" not in tool.spec.input_schema["properties"]
    # 技能不再有分组：任何分组参数都只会把技能藏进能力目录扫不到的子树。
    assert "category" not in tool.spec.input_schema["properties"]


@pytest.mark.asyncio
async def test_skill_view_accepts_the_display_name_from_the_system_prompt(
    tmp_path: Path,
) -> None:
    """prompt 只注入 display_name，所以 skill_view 必须认它，否则「看得见、打不开」。"""
    root = _sandbox(tmp_path)
    display_named = root / "skills/appcli-router"
    display_named.mkdir(parents=True)
    (display_named / "SKILL.md").write_text(
        "---\nname: appcli-router\ndisplay_name: AppCLI Artifact Router\n"
        "description: Route deliverables\n---\n\nRoute body.\n",
        encoding="utf-8",
    )
    call = ToolCallContext("run", "session", "agent")
    tool = _load(root, "skill_view", tmp_path / "workspace")

    viewed = json.loads(
        (await tool.handler(call, {"name": "AppCLI Artifact Router"})).content
    )
    assert viewed["skill_root"] == str(display_named)
    assert "Route body." in viewed["content"]

    folded = json.loads(
        (await tool.handler(call, {"name": "appcli  artifact ROUTER"})).content
    )
    assert folded["skill_root"] == str(display_named)

    missing = json.loads((await tool.handler(call, {"name": "No Such Skill"})).content)
    assert missing["code"] == "not_found"


@pytest.mark.asyncio
async def test_skill_view_resolves_display_name_in_a_pinned_task_snapshot(
    tmp_path: Path,
) -> None:
    """任务快照目录名是 generation-xxx/2，只有 frontmatter 能提供 display_name。"""
    from bianbu_agent.skills import DirectorySkillLoader
    from bianbu_agent.skills.versions import task_skills

    root = _sandbox(tmp_path)
    snapshot = tmp_path / "pinned" / "generation-abc123" / "2"
    shutil.copytree(root / "skills" / "example-skill", snapshot)
    (snapshot / "SKILL.md").write_text(
        "---\nname: example-skill\ndisplay_name: Example Skill\n"
        "description: Example description\n---\n\nSnapshot body.\n",
        encoding="utf-8",
    )
    call = ToolCallContext("run", "session", "agent")
    token = task_skills.set(
        {"example-skill": DirectorySkillLoader().load(snapshot)}
    )
    try:
        viewed = json.loads(
            (await _load(root, "skill_view", tmp_path).handler(
                call, {"name": "Example Skill"},
            )).content
        )
        listed = json.loads(
            (await _load(root, "skills_list", tmp_path).handler(call, {})).content
        )
    finally:
        task_skills.reset(token)

    assert viewed["skill_root"] == str(snapshot)
    assert "Snapshot body." in viewed["content"]
    assert [item["name"] for item in listed["skills"]] == ["example-skill"]


@pytest.mark.asyncio
async def test_exact_skill_name_wins_over_a_foreign_display_name(
    tmp_path: Path,
) -> None:
    root = _sandbox(tmp_path)
    shadow = root / "skills/shadow"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text(
        "---\nname: shadow\ndisplay_name: example-skill\n"
        "description: Shadow\n---\n\nShadow body.\n",
        encoding="utf-8",
    )

    viewed = json.loads(
        (await _load(root, "skill_view", tmp_path / "workspace").handler(
            ToolCallContext("run", "session", "agent"), {"name": "example-skill"},
        )).content
    )

    assert viewed["skill_root"] == str(root / "skills/example-skill")


@pytest.mark.asyncio
async def test_skill_manage_owns_create_patch_and_supporting_files(
    tmp_path: Path,
) -> None:
    root = _sandbox(tmp_path)
    workspace = tmp_path / "workspace"
    call = ToolCallContext("run", "session", "agent")
    tool = _load(root, "skill_manage", workspace)

    created = await tool.handler(
        call,
        {
            "action": "create",
            "name": "new-skill",
            "content": "---\nname: new-skill\ndescription: New\n---\n\nOld body.\n",
        },
    )
    patched = await tool.handler(
        call,
        {
            "action": "patch",
            "name": "new-skill",
            "old_string": "Old body.",
            "new_string": "New body.",
        },
    )
    supporting = await tool.handler(
        call,
        {
            "action": "write_file",
            "name": "new-skill",
            "file_path": "references/note.md",
            "file_content": "reference",
        },
    )

    assert json.loads(created.content)["success"] is True
    assert json.loads(patched.content)["success"] is True
    assert json.loads(supporting.content)["success"] is True
    assert (root / "skills/new-skill/references/note.md").read_text() == "reference"
    assert "New body." in (root / "skills/new-skill/SKILL.md").read_text()


@pytest.mark.asyncio
async def test_skill_manage_creates_skills_where_the_capability_catalog_sees_them(
    tmp_path: Path,
) -> None:
    """用户 Skill 只能落在 skills/<name>，否则能力目录/技能页/专家选择都看不见它。

    回归点：create 曾经接受 category 并写进 skills/<分组>/<name>，而
    `CapabilityInstaller.list()`（控制台 /capabilities/skill、专家技能选择都用它）
    只扫一层，于是"创建成功"的技能在界面上根本不存在。
    """

    from bianbu_agent.home import AgentHome
    from bianbu_agent.skills.manager import SkillPackageManager

    root = _sandbox(tmp_path)
    tool = _load(root, "skill_manage", tmp_path / "workspace")
    created = await tool.handler(
        ToolCallContext("run", "session", "agent"),
        {
            "action": "create",
            "name": "hello-greeting",
            # 旧调用方可能还在传分组；多出来的参数不能再把技能藏起来。
            "category": "custom",
            "content": "---\nname: hello-greeting\ndescription: Greets\n---\n\nHi.\n",
        },
    )

    assert json.loads(created.content)["success"] is True
    assert (root / "skills" / "hello-greeting" / "SKILL.md").is_file()
    assert not (root / "skills" / "custom").exists()
    manager = SkillPackageManager(AgentHome(root))
    assert "hello-greeting" in [record.capability_id for record in manager.list()]
    assert manager.verify("hello-greeting") is True


@pytest.mark.asyncio
async def test_skill_manage_still_reaches_skills_in_legacy_group_directories(
    tmp_path: Path,
) -> None:
    """早期被写进 skills/<分组>/<name> 的技能还要能被 patch/delete 找到。"""

    root = _sandbox(tmp_path)
    legacy = root / "skills" / "custom" / "legacy-skill"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(
        "---\nname: legacy-skill\ndescription: Legacy\n---\n\nOld body.\n",
        encoding="utf-8",
    )
    tool = _load(root, "skill_manage", tmp_path / "workspace")
    patched = await tool.handler(
        ToolCallContext("run", "session", "agent"),
        {"action": "patch", "name": "legacy-skill", "old_string": "Old body.", "new_string": "New body."},
    )

    assert json.loads(patched.content)["success"] is True
    assert "New body." in (legacy / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_skill_view_rejects_supporting_file_traversal(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    result = await _load(root, "skill_view", tmp_path / "workspace").handler(
        ToolCallContext("run", "session", "agent"),
        {"name": "example-skill", "file_path": "../../secret"},
    )

    assert json.loads(result.content)["code"] == "invalid_path"


@pytest.mark.asyncio
@pytest.mark.parametrize("mapped", [False, True])
async def test_user_skills_outside_data_root_are_listed_and_viewable(
    tmp_path: Path, monkeypatch, mapped: bool,
) -> None:
    root = _sandbox(tmp_path)
    user_root = tmp_path / "home/.bianbu-agent/skills"
    if mapped:
        user_root = tmp_path / "sandbox-mount/user-skills"
        monkeypatch.setenv("BIANBU_USER_SKILLS_DIR", str(user_root))
        hidden = tmp_path / "home/.bianbu-agent/skills/hidden"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text("Hidden host skill")
    skill = user_root / "riscv-porter"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: riscv-porter\ndescription: Port code\n---\nUse scripts/check.py")
    script = skill / "scripts/check.py"
    script.write_text("print('check')")
    call = ToolCallContext("run", "session", "agent")
    listed = json.loads((await _load(root, "skills_list", tmp_path).handler(call, {})).content)
    assert {item["name"] for item in listed["skills"]} == {"example-skill", "riscv-porter"}
    tool = _load(root, "skill_view", tmp_path)
    viewed = json.loads((await tool.handler(call, {"name": "riscv-porter"})).content)
    assert viewed["skill_root"] == str(skill)
    assert viewed["absolute_path"] == str(skill / "SKILL.md")
    assert viewed["linked_files"]["scripts"] == ["scripts/check.py"]
    linked = json.loads((await tool.handler(call, {"name": "riscv-porter", "file_path": "scripts/check.py"})).content)
    assert linked["absolute_path"] == str(script)
    assert linked["content"] == script.read_text()
    escaped = json.loads((await tool.handler(call, {"name": "riscv-porter", "file_path": "../secret"})).content)
    assert escaped["code"] == "invalid_path"


@pytest.mark.asyncio
async def test_user_skill_precedence_is_consistent_between_list_and_view(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    user_skill = tmp_path / "home/.bianbu-agent/skills/example-skill"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(
        "---\nname: example-skill\ndescription: User version\n---\nUser body."
    )
    call = ToolCallContext("run", "session", "agent")
    listed = json.loads((await _load(root, "skills_list", tmp_path).handler(call, {})).content)
    assert listed == {"skills": [{"name": "example-skill", "description": "User version"}], "total_count": 1}
    viewed = json.loads((await _load(root, "skill_view", tmp_path).handler(call, {"name": "example-skill"})).content)
    assert viewed["skill_root"] == str(user_skill)
    assert "User body." in viewed["content"]
    rejected = json.loads((await _load(root, "skills_list", tmp_path).handler(call, {"category": "../"})).content)
    assert rejected["code"] == "invalid_request"


def test_skill_manage_edits_system_skill_as_complete_user_override(tmp_path, monkeypatch):
    from bianbu_agent.tools.skills.skill_manage import _manage

    system = tmp_path / 'system'
    root = system / 'skills/example-skill'
    (root / 'scripts').mkdir(parents=True)
    (root / 'SKILL.md').write_text('Original body')
    (root / 'scripts/helper.py').write_text('print("helper")')
    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(system))
    user = tmp_path / 'user/skills'
    with pytest.raises(ValueError, match='match count'):
        _manage(user, {'action': 'patch', 'name': 'example-skill', 'old_string': 'missing', 'new_string': 'new'})
    assert not (user / 'example-skill').exists()
    _manage(user, {'action': 'patch', 'name': 'example-skill', 'old_string': 'Original', 'new_string': 'Edited'})
    assert (user / 'example-skill/SKILL.md').read_text() == 'Edited body'
    assert (user / 'example-skill/scripts/helper.py').read_text() == 'print("helper")'
    assert (root / 'SKILL.md').read_text() == 'Original body'
    with pytest.raises(ValueError, match='system Skill'):
        _manage(user, {'action': 'delete', 'name': 'example-skill'})


@pytest.mark.asyncio
async def test_skill_view_accepts_the_plugin_handle_its_description_documents(
    tmp_path: Path,
) -> None:
    """工具说明让模型写 ``plugin:skill``，那这个句柄就必须能打开插件 Skill。

    资格 ID 在能力目录、``agent.yaml`` 和任务快照里都是 ``plugin/skill``，只认
    斜杠会让照说明调用的模型直接 ``not_found``。
    """
    from bianbu_agent.skills import DirectorySkillLoader
    from bianbu_agent.skills.versions import task_skills

    root = _sandbox(tmp_path)
    snapshot = tmp_path / "pinned" / "generation-abc123" / "2"
    shutil.copytree(root / "skills" / "example-skill", snapshot)
    call = ToolCallContext("run", "session", "agent")
    tool = _load(root, "skill_view", tmp_path / "workspace")
    token = task_skills.set(
        {"spacemit-power/example-skill": DirectorySkillLoader().load(snapshot)}
    )
    try:
        for handle in ("spacemit-power:example-skill", "spacemit-power/example-skill"):
            viewed = json.loads((await tool.handler(call, {"name": handle})).content)
            assert viewed["skill_root"] == str(snapshot), handle
            assert "Body." in viewed["content"]
        assert json.loads(
            (await tool.handler(call, {"name": "spacemit-power:nope"})).content
        )["code"] == "not_found"
    finally:
        task_skills.reset(token)


@pytest.mark.asyncio
async def test_skill_view_plugin_handle_resolves_a_nested_skill_directory(
    tmp_path: Path,
) -> None:
    """插件 Skill 落盘在 ``skills/<plugin>/<skill>/``，同一个句柄也要能打开它。"""
    root = _sandbox(tmp_path)
    nested = root / "skills" / "spacemit-power" / "example-skill"
    shutil.copytree(root / "skills" / "example-skill", nested)
    call = ToolCallContext("run", "session", "agent")
    tool = _load(root, "skill_view", tmp_path / "workspace")

    for handle in ("spacemit-power:example-skill", "spacemit-power/example-skill"):
        viewed = json.loads((await tool.handler(call, {"name": handle})).content)
        assert viewed["skill_root"] == str(nested), handle
