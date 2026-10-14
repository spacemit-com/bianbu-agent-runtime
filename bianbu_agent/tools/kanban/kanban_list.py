import json
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_list(
        assignee: str = None,
        board: str = None,
        include_archived: bool = None,
        limit: int = None,
        status: str = None,
        tenant: str = None,
    ):
        """按负责人、状态或项目命名空间列出持久化 Kanban 任务。

        Args:
            assignee: Optional assignee/profile filter.
            board: 可选看板标识；省略时使用默认看板。
            include_archived: Include archived tasks. Defaults to false.
            limit: Optional maximum rows to return (default 50, max 200).
            status: Optional task status filter.
            tenant: Optional tenant/project namespace filter."""
        arguments = {
            key: value
            for key, value in {
                "assignee": assignee,
                "board": board,
                "include_archived": include_archived,
                "limit": limit,
                "status": status,
                "tenant": tenant,
            }.items()
            if value is not None
        }

        def select(state):
            tasks = list(state.setdefault("tasks", {}).values())
            for field in ("assignee", "status", "tenant", "board"):
                if arguments.get(field) is not None:
                    tasks = [
                        task for task in tasks if task.get(field) == arguments[field]
                    ]
            if not arguments.get("include_archived"):
                tasks = [task for task in tasks if task.get("status") != "archived"]
            tasks.sort(
                key=lambda task: (
                    -int(task.get("priority", 0)),
                    task.get("created_at", ""),
                )
            )
            limit = min(200, max(1, int(arguments.get("limit", 50))))
            return {"tasks": tasks[:limit], "total_count": len(tasks)}

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            select,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_list
