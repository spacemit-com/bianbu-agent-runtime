import json
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_attachments(task_id: str, board: str = None):
        """列出 Kanban 任务已经保存的附件元数据和本地路径。

        Args:
            task_id: 要操作的 Kanban 任务 ID（必填）。
            board: 可选看板标识；省略时使用默认看板。"""
        arguments = {
            key: value
            for key, value in {"task_id": task_id, "board": board}.items()
            if value is not None
        }

        def list_attachments(state):
            task = state.setdefault("tasks", {}).get(str(arguments.get("task_id", "")))
            if not task:
                return {
                    "success": False,
                    "code": "not_found",
                    "error": "Kanban task not found",
                }
            return {
                "attachments": [
                    {
                        key: value
                        for key, value in item.items()
                        if key != "content_base64"
                    }
                    for item in task.get("attachments", [])
                ]
            }

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            list_attachments,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_attachments
