import json
from datetime import datetime, timezone
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_comment(body: str, task_id: str, board: str = None):
        """向 Kanban 任务追加可持久保留的 Markdown 评论。

        Args:
            body: Markdown-supported comment body.
            task_id: Task id. Required (may be your own task or another's — comment threads are per-task).
            board: 可选看板标识；省略时使用默认看板。"""
        arguments = {
            key: value
            for key, value in {"body": body, "task_id": task_id, "board": board}.items()
            if value is not None
        }

        def comment(state):
            task = state.setdefault("tasks", {}).get(str(arguments.get("task_id", "")))
            if not task:
                return {
                    "success": False,
                    "code": "not_found",
                    "error": "Kanban task not found",
                }
            item = {
                "body": str(arguments.get("body", "")),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            task.setdefault("comments", []).append(item)
            return {"task": task, "comment": item}

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            comment,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_comment
