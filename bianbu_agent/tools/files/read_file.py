from __future__ import annotations
import asyncio
import json
from pathlib import Path
from bianbu_agent.tools import tool


def _resolve(workspace: Path, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("path must be a non-empty string")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise PermissionError("path is outside the Tool workspace")
    return resolved


def _read(path: Path, offset: int, limit: int) -> dict[str, object]:
    if not path.is_file():
        return {"error": f"File not found: {path}"}
    raw = path.read_bytes()
    if b"\x00" in raw[:1000]:
        return {
            "is_binary": True,
            "file_size": len(raw),
            "error": "Binary file - cannot display as text.",
        }
    lines = raw.decode("utf-8-sig").splitlines()
    page = lines[offset - 1 : offset - 1 + limit]
    end = offset + len(page) - 1
    result: dict[str, object] = {
        "content": "\n".join(
            (
                f"{line_number}|{line}"
                for line_number, line in enumerate(page, start=offset)
            )
        ),
        "total_lines": len(lines),
        "file_size": len(raw),
        "truncated": len(lines) > offset - 1 + limit,
    }
    if result["truncated"]:
        result["hint"] = (
            f"Use offset={end + 1} to continue reading (showing {offset}-{end} of {len(lines)} lines)"
        )
    return result


def create_tool(context):

    @tool(parse_docstring=True)
    async def read_file(path: str, offset: int = 1, limit: int = 500) -> str:
        """Read a UTF-8 text file from the current Tool workspace.

        Args:
            path: Absolute or workspace-relative file path.
            offset: First line to return, using one-based numbering.
            limit: Maximum number of lines to return, capped at 2000.
        """
        try:
            resolved = _resolve(context.workspace_root, path)
            payload = await asyncio.to_thread(
                _read, resolved, max(1, offset), min(2000, max(1, limit))
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

    return read_file
