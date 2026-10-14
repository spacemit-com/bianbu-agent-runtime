from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from bianbu_agent.tools import tool
from bianbu_agent._internal.locking import CapabilityFilesystemLock
from bianbu_agent.system_resources import system_resource_dir

_NAME = re.compile("[a-z0-9][a-z0-9_-]{0,63}")
_SUPPORT_DIRS = frozenset({"references", "templates", "scripts", "assets"})


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".skill-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _support_path(skill_root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("file_path is required")
    path = (skill_root / value).resolve()
    relative = path.relative_to(skill_root.resolve())
    if not relative.parts or relative.parts[0] not in _SUPPORT_DIRS:
        raise ValueError(
            "file_path must be under references/, templates/, scripts/, or assets/"
        )
    return path


def _manage(skills_root: Path, arguments: dict[str, object]) -> dict[str, object]:
    with CapabilityFilesystemLock(skills_root.parent, exclusive=True):
        return _manage_locked(skills_root, arguments)


def _manage_locked(skills_root: Path, arguments: dict[str, object]) -> dict[str, object]:
    action = str(arguments.get("action", ""))
    name = arguments.get("name")
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ValueError("name must contain only lowercase letters, digits, '-' or '_'")
    # 用户 Skill 只有一个位置：skills/<name>。能力目录、控制台技能页和专家技能
    # 选择都只扫这一层，写进 skills/<分组>/<name> 会让技能对它们彻底不可见。
    skill_root = skills_root / name
    if action != "create" and (not skill_root.is_dir()):
        # 兼容早期写进 skills/<分组>/<name> 的技能：它们对能力目录不可见，但仍然
        # 能被 patch/edit/delete 找到，用户不用手工搬目录。
        matches = [
            path.parent
            for path in skills_root.rglob("SKILL.md")
            if path.parent.name == name
        ]
        if len(matches) == 1:
            skill_root = matches[0]
    packaged = system_resource_dir("skills") / name
    if action == "delete" and packaged.exists():
        raise ValueError("system Skill cannot be deleted")
    if action in {"edit", "patch", "write_file", "remove_file"} and not skill_root.exists() and packaged.is_dir():
        # Edit the full default package in staging first. Failed edits leave no
        # user override; successful edits retain scripts and reference files.
        skills_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".skill-override-", dir=skills_root.parent) as temporary:
            staged_root = Path(temporary)
            shutil.copytree(packaged, staged_root / name)
            result = _manage_locked(staged_root, arguments)
            os.rename(staged_root / name, skill_root)
            result["files_modified"] = [
                str(skill_root / Path(path).relative_to(staged_root / name))
                for path in result["files_modified"]
            ]
            return result
    if action == "create":
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content is required")
        if skill_root.exists():
            raise ValueError(f"Skill already exists: {name}")
        skill_root.mkdir(parents=True)
        _write(skill_root / "SKILL.md", content)
        files = [str(skill_root / "SKILL.md")]
    elif action == "edit":
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content is required")
        _write(skill_root / "SKILL.md", content)
        files = [str(skill_root / "SKILL.md")]
    elif action == "patch":
        target = (
            skill_root / "SKILL.md"
            if not arguments.get("file_path")
            else _support_path(skill_root, arguments.get("file_path"))
        )
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or (not old):
            raise ValueError("old_string and new_string are required")
        content = target.read_text(encoding="utf-8")
        count = content.count(old)
        if count == 0 or (count > 1 and (not arguments.get("replace_all"))):
            raise ValueError(f"old_string match count is {count}")
        _write(
            target, content.replace(old, new, -1 if arguments.get("replace_all") else 1)
        )
        files = [str(target)]
    elif action == "write_file":
        target = _support_path(skill_root, arguments.get("file_path"))
        content = arguments.get("file_content")
        if not isinstance(content, str):
            raise ValueError("file_content is required")
        _write(target, content)
        files = [str(target)]
    elif action == "remove_file":
        target = _support_path(skill_root, arguments.get("file_path"))
        target.unlink()
        files = [str(target)]
    elif action == "delete":
        shutil.rmtree(skill_root)
        files = [str(skill_root)]
    else:
        raise ValueError(f"unsupported action: {action}")
    return {"success": True, "action": action, "name": name, "files_modified": files}


def create_tool(package_context):
    skills_root = package_context.home_root / "skills"

    @tool(parse_docstring=True)
    async def skill_manage(
        action: str,
        name: str,
        content: str = None,
        file_content: str = None,
        file_path: str = None,
        new_string: str = None,
        old_string: str = None,
        replace_all: bool = None,
    ):
        """Manage skills (create, update, delete). Skills are your procedural memory — reusable approaches for recurring task types. New skills and edits go to the Runtime user skills directory. Editing a system skill creates a complete user override; system skills cannot be deleted.

        Actions: create (full SKILL.md), patch (old_string/new_string — preferred for fixes), edit (full SKILL.md rewrite — major overhauls only), delete, write_file, remove_file.

        Create when: complex task succeeded (5+ calls), errors overcome, user-corrected approach worked, non-trivial workflow discovered, or user asks you to remember a procedure.
        Update when: instructions stale/wrong, OS-specific failures, missing steps or pitfalls found during use. If you used a skill and hit issues not covered by it, patch it immediately.

        After difficult/iterative tasks, offer to save as a skill. Skip for simple one-offs. Confirm with user before creating/deleting.

        Good skills: trigger conditions, numbered steps with exact commands, pitfalls section, verification steps. Use skill_view() to see format examples.

        Args:
            action: The action to perform.
            name: Skill name (lowercase, hyphens/underscores, max 64 chars). Must match an existing skill for patch/edit/delete/write_file/remove_file.
            content: Full SKILL.md content (YAML frontmatter + markdown body). Required for 'create' and 'edit'. For 'edit', read the skill first with skill_view() and provide the complete updated text.
            file_content: Content for the file. Required for 'write_file'.
            file_path: Path to a supporting file within the skill directory. For 'write_file'/'remove_file': required, must be under references/, templates/, scripts/, or assets/. For 'patch': optional, defaults to SKILL.md if omitted.
            new_string: Replacement text (required for 'patch'). Can be empty string to delete the matched text.
            old_string: Text to find in the file (required for 'patch'). Must be unique unless replace_all=true. Include enough surrounding context to ensure uniqueness.
            replace_all: For 'patch': replace all occurrences instead of requiring a unique match (default: false).
        """
        arguments = {
            key: value
            for key, value in {
                "action": action,
                "name": name,
                "content": content,
                "file_content": file_content,
                "file_path": file_path,
                "new_string": new_string,
                "old_string": old_string,
                "replace_all": replace_all,
            }.items()
            if value is not None
        }
        try:
            payload = await asyncio.to_thread(_manage, skills_root, dict(arguments))
        except (OSError, TypeError, ValueError) as exc:
            payload = {"success": False, "code": "invalid_request", "error": str(exc)}
        return json.dumps(payload, ensure_ascii=False)

    return skill_manage
