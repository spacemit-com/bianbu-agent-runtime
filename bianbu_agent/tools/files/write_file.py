from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import tempfile
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


def _atomic_write(path: Path, content: str) -> tuple[int, bool]:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".bianbu-agent-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return (len(content.encode("utf-8")), not parent_existed)


def create_tool(context):

    @tool(parse_docstring=True)
    async def write_file(path: str, content: str) -> str:
        """Atomically write UTF-8 text inside the current Tool workspace.

        Args:
            path: Absolute or workspace-relative destination path.
            content: Complete text content to write.
        """
        try:
            resolved = _resolve(context.workspace_root, path)
            bytes_written, dirs_created = await asyncio.to_thread(
                _atomic_write, resolved, content
            )
            payload = {
                "bytes_written": bytes_written,
                "dirs_created": dirs_created,
                "resolved_path": str(resolved),
                "files_modified": [str(resolved)],
            }
        except PermissionError as exc:
            payload = {
                "success": False,
                "code": "path_outside_workspace",
                "error": str(exc),
            }
        except (OSError, TypeError, ValueError) as exc:
            payload = {"success": False, "code": "invalid_request", "error": str(exc)}
        return json.dumps(payload, ensure_ascii=False)

    return write_file
