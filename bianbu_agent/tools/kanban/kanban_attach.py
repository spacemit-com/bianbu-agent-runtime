import base64
import binascii
import json
from pathlib import Path
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_attach(
        content_base64: str,
        filename: str,
        task_id: str,
        board: str = None,
        content_type: str = None,
    ):
        """把 Base64 编码的文件作为附件保存到 Kanban 任务。

        Args:
            content_base64: The file contents, base64-encoded. Max 25 MB decoded.
            filename: File name to store it under (e.g. 'report.pdf'). Directory components are stripped; only the leaf is kept.
            task_id: 要操作的 Kanban 任务 ID（必填）。
            board: 可选看板标识；省略时使用默认看板。
            content_type: Optional MIME type (e.g. 'application/pdf')."""
        arguments = {
            key: value
            for key, value in {
                "content_base64": content_base64,
                "filename": filename,
                "task_id": task_id,
                "board": board,
                "content_type": content_type,
            }.items()
            if value is not None
        }
        try:
            content = base64.b64decode(
                str(arguments.get("content_base64", "")), validate=True
            )
            if len(content) > 25 * 1024 * 1024:
                raise ValueError("attachment exceeds 25 MB")
        except (binascii.Error, ValueError) as exc:
            return json.dumps(
                {"success": False, "code": "invalid_attachment", "error": str(exc)}
            )
        filename = Path(str(arguments.get("filename", "attachment"))).name

        def attach(state):
            task = state.setdefault("tasks", {}).get(str(arguments.get("task_id", "")))
            if not task:
                return {
                    "success": False,
                    "code": "not_found",
                    "error": "Kanban task not found",
                }
            item = {
                "filename": filename,
                "content_type": arguments.get("content_type"),
                "size": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
            task.setdefault("attachments", []).append(item)
            return {
                "attachment": {
                    key: value for key, value in item.items() if key != "content_base64"
                }
            }

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            attach,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_attach
