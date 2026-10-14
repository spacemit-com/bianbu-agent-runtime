import json
from datetime import datetime, timezone
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_heartbeat(task_id: str, board: str = None, note: str = None):
        """为长时间执行的 Kanban 任务记录存活心跳和简短进度。

        Args:
            task_id: 要操作的 Kanban 任务 ID（必填）。
            board: 可选看板标识；省略时使用默认看板。
            note: Optional short note describing current progress. Shown in the event log.
        """
        arguments = {
            key: value
            for key, value in {"task_id": task_id, "board": board, "note": note}.items()
            if value is not None
        }

        def heartbeat(state):
            task = state.setdefault("tasks", {}).get(str(arguments.get("task_id", "")))
            if not task:
                return {
                    "success": False,
                    "code": "not_found",
                    "error": "Kanban task not found",
                }
            if task.get("status") == "ready":
                task["status"] = "running"
            task["heartbeat"] = str(arguments.get("note", ""))
            task["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
            return {"task": task}

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            heartbeat,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_heartbeat
