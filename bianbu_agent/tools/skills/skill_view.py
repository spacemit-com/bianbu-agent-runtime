from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from bianbu_agent.system_resources import system_resource_dir
from bianbu_agent.tools import tool, get_task_skills


def _fold(text: object) -> str:
    """空白折叠 + 大小写折叠，用于 display_name 这类人类可读句柄的容错匹配。"""
    return " ".join(str(text).split()).casefold()


def _find_skill(skills_root: Path, name: str) -> Path | None:
    for path in skills_root.rglob("SKILL.md") if skills_root.is_dir() else ():
        if (
            path.parent.name == name
            or str(path.parent.relative_to(skills_root)) == name
        ):
            return path.parent
    return None


def _display_name(skill_root: Path) -> str:
    """读 SKILL.md frontmatter 的 display_name；解析失败就当没有。

    目录里可能躺着不是本 Runtime 格式的 SKILL.md（例如用户手工拷贝的
    上游技能），一个坏文件不该让整次查找失败。
    """
    from bianbu_agent.skills.loader import _parse_skill

    try:
        metadata, _ = _parse_skill((skill_root / "SKILL.md").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    return str(metadata.get("display_name") or "").strip()


def _find_skill_by_display_name(skills_root: Path, name: str) -> Path | None:
    folded = _fold(name)
    if not folded:
        return None
    for path in skills_root.rglob("SKILL.md") if skills_root.is_dir() else ():
        display = _display_name(path.parent)
        if display and (display == name or _fold(display) == folded):
            return path.parent
    return None


def _pinned_skill(pinned: dict, name: str) -> dict | None:
    """在任务快照里按 Skill 名或 display_name 找一个 Skill。

    快照目录名是 ``generation-xxxx/1`` 这类内部路径，所以 pinned 分支既不能
    靠目录名匹配，也不能只认 frontmatter name：prompt 里的句柄是 display_name。
    """
    skill = pinned.get(name)
    if skill is not None:
        return skill
    folded = _fold(name)
    for value in pinned.values():
        display = str(value.get("display_name") or "")
        if display and (display == name or _fold(display) == folded):
            return value
    return None


def _skill_handles(name: str) -> tuple[str, ...]:
    """把工具说明承诺的 ``plugin:skill`` 句柄补成 Runtime 的 ``plugin/skill``。

    插件成员的资格 ID 处处是 ``plugin/member``（`plugin.yaml` 贡献、能力目录、
    ``agent.yaml`` 的 skills/tools/mcps、任务快照的键），只有工具说明告诉模型写
    ``plugin:skill``。只认斜杠会让按说明调用的模型直接 ``not_found`` —— 专家
    「看得见 Skill、打不开 Skill」。两个形式都认，斜杠仍是权威写法。
    """

    text = name.strip()
    handles = [text]
    if "/" not in text and ":" in text:
        namespace, _, local = text.partition(":")
        if namespace.strip() and local.strip():
            handles.append(f"{namespace.strip()}/{local.strip()}")
    return tuple(dict.fromkeys(handles))


def _view(skills_roots: tuple[Path, ...], name: object, file_path: object, pinned=None) -> dict[str, object]:
    if not isinstance(name, str) or not name.strip():
        return {
            "success": False,
            "code": "invalid_request",
            "error": "name is required",
        }
    skill_root = None
    for handle in _skill_handles(name):
        if pinned is not None:
            skill = _pinned_skill(pinned, handle)
            manifest = skill.get("manifest_path") if skill else None
            skill_root = Path(manifest).parent if manifest else None
        else:
            # 两趟：先按目录名/相对路径精确匹配（它是稳定句柄），再退回
            # display_name。混在一趟会让某个 Skill 的 display_name 抢走另一个
            # 根目录下真正同名的 Skill。
            skill_root = None
            for matcher in (_find_skill, _find_skill_by_display_name):
                skill_root = next(
                    (found for root in skills_roots
                     if (found := matcher(root, handle)) is not None),
                    None,
                )
                if skill_root is not None:
                    break
        if skill_root is not None:
            break
    if skill_root is None:
        return {
            "success": False,
            "code": "not_found",
            "error": f"Skill not found: {name}",
        }
    relative = "SKILL.md" if file_path in (None, "") else str(file_path)
    target = (skill_root / relative).resolve()
    if not target.is_relative_to(skill_root.resolve()):
        return {
            "success": False,
            "code": "invalid_path",
            "error": "file_path escapes the Skill directory",
        }
    if not target.is_file():
        return {
            "success": False,
            "code": "not_found",
            "error": f"Skill file not found: {relative}",
        }
    payload: dict[str, object] = {
        "name": name,
        "file_path": relative,
        "skill_root": str(skill_root.resolve()),
        "absolute_path": str(target),
        "content": target.read_text(encoding="utf-8"),
    }
    if relative == "SKILL.md":
        payload["linked_files"] = {
            directory: sorted(
                str(path.relative_to(skill_root))
                for path in (skill_root / directory).rglob("*")
                if path.is_file() and not path.is_symlink()
            )
            if (skill_root / directory).is_dir()
            else []
            for directory in ("references", "templates", "scripts", "assets")
        }
    return payload


def create_tool(package_context):
    # The worker supplies the trusted sandbox mapping when HOME is isolated.
    skills_roots = tuple(dict.fromkeys((
        Path(os.environ.get("BIANBU_USER_SKILLS_DIR")
             or Path.home() / ".bianbu-agent" / "skills").resolve(),
        (package_context.home_root / "skills").resolve(),
        system_resource_dir("skills"),
    )))

    @tool(parse_docstring=True)
    async def skill_view(name: str, file_path: str = None):
        """Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. Load a skill's full content or access its linked files (references, templates, scripts). First call returns SKILL.md content plus a 'linked_files' dict showing available references/templates/scripts. To access those, call again with file_path parameter. The returned skill_root and absolute_path are absolute paths in the current execution environment. Resolve scripts and other relative skill paths against skill_root; use that directory as cwd when a script requires skill-relative resources.

        Args:
            name: The skill name, either the directory name from skills_list or the display name shown in the system prompt (both resolve to the same Skill). For plugin-provided skills, use the qualified ID 'plugin/skill' (e.g. 'superpowers/writing-skills'); the 'plugin:skill' spelling resolves to the same Skill.
            file_path: OPTIONAL: Path to a linked file within the skill (e.g., 'references/api.md', 'templates/config.yaml', 'scripts/validate.py'). Omit to get the main SKILL.md content.
        """
        arguments = {
            key: value
            for key, value in {"name": name, "file_path": file_path}.items()
            if value is not None
        }
        name = arguments.get("name")
        payload = await asyncio.to_thread(
            _view, skills_roots, name, arguments.get("file_path"),
            get_task_skills(),
        )
        return json.dumps(payload, ensure_ascii=False)

    return skill_view
