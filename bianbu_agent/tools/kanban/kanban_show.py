import json
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_show(task_id: str, board: str = None):
        """读取一项 Kanban 任务的完整状态、依赖、评论、运行记录和最近事件。

        Args:
            task_id: 要操作的 Kanban 任务 ID（必填）。
            board: 可选看板标识；省略时使用默认看板。"""
        arguments = {
            key: value
            for key, value in {"task_id": task_id, "board": board}.items()
            if value is not None
        }

        def show(state):
            task = state.setdefault("tasks", {}).get(str(arguments.get("task_id", "")))
            return (
                {"task": task}
                if task
                else {
                    "success": False,
                    "code": "not_found",
                    "error": "Kanban task not found",
                }
            )

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            show,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_show
