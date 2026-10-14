from __future__ import annotations
import asyncio
import json
import os
import re
from pathlib import Path
from bianbu_agent.system_resources import system_resource_dir
from bianbu_agent.tools import tool, get_task_skills


def _metadata(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    name_match = re.search("(?m)^name:\\s*['\\\"]?([^'\\\"\\n]+)", text)
    description_match = re.search("(?m)^description:\\s*['\\\"]?([^'\\\"\\n]+)", text)
    return {
        "name": name_match.group(1).strip() if name_match else path.parent.name,
        "description": description_match.group(1).strip() if description_match else "",
    }


def _list(skills_root: Path, category: object, allowed: tuple[str, ...] | None):
    selected_root = skills_root
    if isinstance(category, str) and category.strip():
        candidate = (skills_root / category.strip()).resolve()
        if not candidate.is_relative_to(skills_root.resolve()):
            raise ValueError("invalid category")
        selected_root = candidate
    skills = []
    if selected_root.is_dir():
        for path in sorted(selected_root.rglob("SKILL.md")):
            item = _metadata(path)
            if allowed is None or item["name"] in allowed:
                skills.append(item)
    return {"skills": skills, "total_count": len(skills)}


def _list_roots(skills_roots: tuple[Path, ...], category: object, pinned=None):
    if pinned is not None:
        skills = [{"name": name, "description": skill["description"]}
                  for name, skill in pinned.items()
                  if not category or name.startswith(str(category).strip() + "/")]
        return {"skills": skills, "total_count": len(skills)}
    skills = {}
    for root in skills_roots:
        for item in _list(root, category, None)["skills"]:
            skills.setdefault(item["name"], item)
    return {"skills": list(skills.values()), "total_count": len(skills)}


def create_tool(package_context):
    # Match skill_view and Runtime discovery, including the trusted sandbox mount.
    skills_roots = tuple(dict.fromkeys((
        Path(os.environ.get("BIANBU_USER_SKILLS_DIR")
             or Path.home() / ".bianbu-agent" / "skills").resolve(),
        (package_context.home_root / "skills").resolve(),
        system_resource_dir("skills"),
    )))

    @tool(parse_docstring=True)
    async def skills_list(category: str = None):
        """List available skills (name + description). Use skill_view(name) to load full content.

        Args:
            category: Optional category filter to narrow results"""
        arguments = {
            key: value
            for key, value in {"category": category}.items()
            if value is not None
        }
        try:
            payload = await asyncio.to_thread(
                _list_roots,
                skills_roots,
                arguments.get("category"),
                get_task_skills(),
            )
        except (OSError, ValueError) as exc:
            payload = {"success": False, "code": "invalid_request", "error": str(exc)}
        return json.dumps(payload, ensure_ascii=False)

    return skills_list
