import json
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_block(
        reason: str, task_id: str, board: str = None, kind: str = None
    ):
        """记录真实阻塞原因，并把 Kanban 任务转为阻塞状态。

        Args:
            reason: What you need answered or what stopped you, in one or two sentences. Don't paste the whole conversation; the human has the board and can ask follow-ups via comments.
            task_id: 要操作的 Kanban 任务 ID（必填）。
            board: 可选看板标识；省略时使用默认看板。
            kind: Why you're blocked. 'dependency' waits in todo and resumes automatically; the others surface to a human. Omit only if none apply.
        """
        arguments = {
            key: value
            for key, value in {
                "reason": reason,
                "task_id": task_id,
                "board": board,
                "kind": kind,
            }.items()
            if value is not None
        }

        def block(state):
            task = state.setdefault("tasks", {}).get(str(arguments.get("task_id", "")))
            if not task:
                return {
                    "success": False,
                    "code": "not_found",
                    "error": "Kanban task not found",
                }
            task.update(
                status="blocked",
                blocked_reason=str(arguments.get("reason", "")),
                blocked_kind=arguments.get("kind"),
            )
            return {"task": task}

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            block,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_block
