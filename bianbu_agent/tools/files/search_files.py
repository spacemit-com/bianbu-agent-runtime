from __future__ import annotations
import asyncio
import fnmatch
import json
import re
from pathlib import Path
from bianbu_agent.tools import tool


def _resolve(workspace: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str):
        raise ValueError("path must be a string")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise PermissionError("path is outside the Tool workspace")
    return resolved


def _iter_files(workspace: Path, root: Path):
    if root.is_file():
        yield root
        return
    for path in root.rglob("*"):
        if (
            path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(workspace.resolve())
            and (
                not any((part.startswith(".") for part in path.relative_to(root).parts))
            )
        ):
            yield path


def _search(
    workspace: Path,
    root: Path,
    pattern: str,
    target: str,
    file_glob: str | None,
    limit: int,
    offset: int,
    output_mode: str,
) -> dict[str, object]:
    if not root.exists():
        return {"total_count": 0, "error": f"Path not found: {root}"}
    files = list(_iter_files(workspace, root))
    if target == "files":
        found = sorted(
            (
                str(path.relative_to(workspace))
                for path in files
                if fnmatch.fnmatch(path.name, pattern)
            )
        )
        page = found[offset : offset + limit]
        return {
            "total_count": len(found),
            "files": page,
            "truncated": len(found) > offset + limit,
        }
    expression = re.compile(pattern)
    matches: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    for path in sorted(files):
        relative = str(path.relative_to(workspace))
        if file_glob and (not fnmatch.fnmatch(path.name, file_glob)):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError, OSError:
            continue
        for number, line in enumerate(lines, start=1):
            if expression.search(line):
                matches.append({"path": relative, "line": number, "content": line})
                counts[relative] = counts.get(relative, 0) + 1
    total = len(matches)
    page = matches[offset : offset + limit]
    result: dict[str, object]
    if output_mode == "files_only":
        files_only = sorted(counts)
        result = {
            "total_count": len(files_only),
            "files": files_only[offset : offset + limit],
            "truncated": len(files_only) > offset + limit,
        }
    elif output_mode == "count":
        result = {"total_count": len(counts), "counts": counts}
    else:
        result = {"total_count": total, "matches": page}
        if total > offset + limit:
            result["truncated"] = True
    return result


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def search_files(
        pattern: str,
        file_glob: str = None,
        limit: int = 50,
        offset: int = 0,
        output_mode: str = "content",
        path: str = ".",
        target: str = "content",
    ):
        """Search file contents or find files by name. Use this instead of grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents.

        Content search (target='content'): Regex search inside files. Output modes: full matches with line numbers, file paths only, or match counts.

        File search (target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls — results sorted by modification time.

        Args:
            pattern: Regex pattern for content search, or glob pattern (e.g., '*.py') for file search
            file_glob: Filter files by pattern in grep mode (e.g., '*.py' to only search Python files)
            limit: Maximum number of results to return (default: 50)
            offset: Skip first N results for pagination (default: 0)
            output_mode: Output format for grep mode: 'content' shows matching lines with line numbers, 'files_only' lists file paths, 'count' shows match counts per file
            path: Directory or file to search in (default: current working directory)
            target: 'content' searches inside file contents, 'files' searches for files by name
        """
        arguments = {
            key: value
            for key, value in {
                "pattern": pattern,
                "file_glob": file_glob,
                "limit": limit,
                "offset": offset,
                "output_mode": output_mode,
                "path": path,
                "target": target,
            }.items()
            if value is not None
        }
        try:
            pattern = arguments.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise ValueError("pattern must be a non-empty string")
            root = _resolve(package_context.workspace_root, arguments.get("path", "."))
            payload = await asyncio.to_thread(
                _search,
                package_context.workspace_root,
                root,
                pattern,
                str(arguments.get("target", "content")),
                arguments.get("file_glob"),
                max(1, int(arguments.get("limit", 50))),
                max(0, int(arguments.get("offset", 0))),
                str(arguments.get("output_mode", "content")),
            )
        except PermissionError as exc:
            payload = {
                "success": False,
                "code": "path_outside_workspace",
                "error": str(exc),
            }
        except (OSError, TypeError, ValueError, re.error) as exc:
            payload = {"success": False, "code": "invalid_request", "error": str(exc)}
        return json.dumps(payload, ensure_ascii=False)

    return search_files
