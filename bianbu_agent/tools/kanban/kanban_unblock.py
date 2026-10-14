import json
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_unblock(task_id: str, board: str = None):
        """把阻塞中的 Kanban 任务恢复到可继续处理状态。

        Args:
            task_id: Blocked task id to move to ready or parent-gated todo.
            board: 可选看板标识；省略时使用默认看板。"""
        arguments = {
            key: value
            for key, value in {"task_id": task_id, "board": board}.items()
            if value is not None
        }

        def unblock(state):
            tasks = state.setdefault("tasks", {})
            task = tasks.get(str(arguments.get("task_id", "")))
            if not task:
                return {
                    "success": False,
                    "code": "not_found",
                    "error": "Kanban task not found",
                }
            task["status"] = (
                "ready"
                if all(
                    (
                        tasks.get(item, {}).get("status") == "done"
                        for item in task.get("parents", [])
                    )
                )
                else "todo"
            )
            task.pop("blocked_reason", None)
            task.pop("blocked_kind", None)
            return {"task": task}

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            unblock,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_unblock
