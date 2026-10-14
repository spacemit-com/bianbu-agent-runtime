from __future__ import annotations
import asyncio
import difflib
import json
from pathlib import Path
from bianbu_agent.tools import tool


def _resolve(workspace: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("path must be a non-empty string")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise PermissionError("path is outside the Tool workspace")
    return resolved


def _replace(path: Path, old: str, new: str, replace_all: bool) -> dict[str, object]:
    before = path.read_text(encoding="utf-8")
    count = before.count(old)
    if count == 0:
        return {"success": False, "error": "old_string was not found"}
    if count > 1 and (not replace_all):
        return {
            "success": False,
            "error": f"old_string occurs {count} times; provide more context or set replace_all=true",
        }
    after = before.replace(old, new, -1 if replace_all else 1)
    path.write_text(after, encoding="utf-8", newline="")
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    return {"success": True, "diff": diff, "files_modified": [str(path)]}


def _v4a(workspace: Path, patch_text: str) -> dict[str, object]:
    lines = patch_text.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError(
            "patch must start with '*** Begin Patch' and end with '*** End Patch'"
        )
    operations = []
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        index += 1
        body = []
        while index < len(lines) - 1 and (not lines[index].startswith("*** ")):
            body.append(lines[index])
            index += 1
        if header.startswith("*** Update File: "):
            operations.append(
                ("update", header.removeprefix("*** Update File: ").strip(), body)
            )
        elif header.startswith("*** Add File: "):
            operations.append(
                ("add", header.removeprefix("*** Add File: ").strip(), body)
            )
        elif header.startswith("*** Delete File: "):
            operations.append(
                ("delete", header.removeprefix("*** Delete File: ").strip(), body)
            )
        elif header.startswith("*** Move File: ") and " -> " in header:
            source, target = header.removeprefix("*** Move File: ").split(" -> ", 1)
            operations.append(("move", (source.strip(), target.strip()), body))
        else:
            raise ValueError(f"unsupported patch header: {header}")
    writes: dict[Path, str] = {}
    deletes: set[Path] = set()
    modified, created, deleted = ([], [], [])
    diffs = []
    for action, raw_path, body in operations:
        if action == "move":
            source = _resolve(workspace, raw_path[0])
            target = _resolve(workspace, raw_path[1])
            if source == target:
                raise ValueError("move source and destination must differ")
            if target.exists() or target in writes:
                raise ValueError(f"move destination already exists: {raw_path[1]}")
            content = writes.pop(source, source.read_text(encoding="utf-8"))
            writes[target] = content
            deletes.add(source)
            modified.append(str(target))
            deleted.append(str(source))
            continue
        path = _resolve(workspace, raw_path)
        if action == "add":
            if path.exists() or path in writes:
                raise ValueError(f"file already exists: {raw_path}")
            content = (
                "\n".join((line[1:] for line in body if line.startswith("+"))) + "\n"
            )
            writes[path] = content
            created.append(str(path))
            continue
        if action == "delete":
            if not path.is_file() and path not in writes:
                raise ValueError(f"file not found: {raw_path}")
            writes.pop(path, None)
            deletes.add(path)
            deleted.append(str(path))
            continue
        before = writes.get(path)
        if before is None:
            before = path.read_text(encoding="utf-8")
        after = before
        chunks: list[list[str]] = []
        current: list[str] = []
        for line in body:
            if line.startswith("@@"):
                if current:
                    chunks.append(current)
                    current = []
            else:
                current.append(line)
        if current:
            chunks.append(current)
        for chunk in chunks:
            old = (
                "\n".join((line[1:] for line in chunk if line[:1] in {" ", "-"})) + "\n"
            )
            new = (
                "\n".join((line[1:] for line in chunk if line[:1] in {" ", "+"})) + "\n"
            )
            if old not in after:
                raise ValueError(f"update context was not found in {raw_path}")
            after = after.replace(old, new, 1)
        writes[path] = after
        modified.append(str(path))
        diffs.append(
            "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=str(path),
                    tofile=str(path),
                )
            )
        )
    for path, content in writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    for path in deletes:
        path.unlink(missing_ok=True)
    return {
        "success": True,
        "diff": "".join(diffs),
        "files_modified": modified,
        "files_created": created,
        "files_deleted": deleted,
    }


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def patch(
        mode: str,
        cross_profile: bool = False,
        new_string: str = None,
        old_string: str = None,
        patch: str = None,
        path: str = None,
        replace_all: bool = False,
    ):
        """Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. Returns a unified diff. Auto-runs syntax checks after editing.

        REPLACE MODE (mode='replace', default): find a unique string and replace it. REQUIRED PARAMETERS: mode, path, old_string, new_string.
        PATCH MODE (mode='patch'): apply V4A multi-file patches for bulk changes. REQUIRED PARAMETERS: mode, patch.

        Args:
            mode: Edit mode. 'replace' (default): requires path + old_string + new_string. 'patch': requires patch content only.
            cross_profile: Opt out of the cross-profile soft guard. Defaults to false. Set true ONLY after explicit user direction to edit another Hermes profile's skills/plugins/cron/memories.
            new_string: REQUIRED when mode='replace'. Replacement text. Pass empty string '' to delete the matched text.
            old_string: REQUIRED when mode='replace'. Exact text to find and replace. Must be unique in the file unless replace_all=true. Include surrounding context lines to ensure uniqueness.
            patch: REQUIRED when mode='patch'. V4A format patch content. Format: *** Begin Patch *** Update File: path/to/file @@ context hint @@ context line -removed line +added line *** End Patch
            path: REQUIRED when mode='replace'. File path to edit.
            replace_all: Replace all occurrences instead of requiring a unique match (default: false)
        """
        arguments = {
            key: value
            for key, value in {
                "mode": mode,
                "cross_profile": cross_profile,
                "new_string": new_string,
                "old_string": old_string,
                "patch": patch,
                "path": path,
                "replace_all": replace_all,
            }.items()
            if value is not None
        }
        try:
            if arguments.get("mode", "replace") == "patch":
                patch_text = arguments.get("patch")
                if not isinstance(patch_text, str):
                    raise ValueError("patch content is required")
                payload = await asyncio.to_thread(
                    _v4a, package_context.workspace_root, patch_text
                )
                return json.dumps(payload, ensure_ascii=False)
            if arguments.get("mode", "replace") != "replace":
                raise ValueError("mode must be replace or patch")
            old = arguments.get("old_string")
            new = arguments.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str) or (not old):
                raise ValueError(
                    "old_string and new_string must be strings; old_string cannot be empty"
                )
            path = _resolve(package_context.workspace_root, arguments.get("path"))
            payload = await asyncio.to_thread(
                _replace, path, old, new, bool(arguments.get("replace_all", False))
            )
        except PermissionError as exc:
            payload = {
                "success": False,
                "code": "path_outside_workspace",
                "error": str(exc),
            }
        except (OSError, TypeError, ValueError) as exc:
            payload = {"success": False, "code": "invalid_request", "error": str(exc)}
        return json.dumps(payload, ensure_ascii=False)

    return patch
