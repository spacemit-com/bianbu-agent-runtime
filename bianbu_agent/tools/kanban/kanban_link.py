import json
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_link(child_id: str, parent_id: str, board: str = None):
        """在两项现有 Kanban 任务之间建立父任务到子任务的依赖关系。

        Args:
            child_id: Child task id.
            parent_id: Parent task id.
            board: 可选看板标识；省略时使用默认看板。"""
        arguments = {
            key: value
            for key, value in {
                "child_id": child_id,
                "parent_id": parent_id,
                "board": board,
            }.items()
            if value is not None
        }

        def link(state):
            tasks = state.setdefault("tasks", {})
            parent_id, child_id = (
                str(arguments.get("parent_id", "")),
                str(arguments.get("child_id", "")),
            )
            if parent_id == child_id or parent_id not in tasks or child_id not in tasks:
                return {
                    "success": False,
                    "code": "invalid_link",
                    "error": "Both distinct Kanban tasks must exist",
                }
            pending = [parent_id]
            visited = set()
            while pending:
                current = pending.pop()
                if current == child_id:
                    return {
                        "success": False,
                        "code": "dependency_cycle",
                        "error": "Kanban dependency links must be acyclic",
                    }
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(tasks.get(current, {}).get("parents", []))
            if parent_id not in tasks[child_id].setdefault("parents", []):
                tasks[child_id]["parents"].append(parent_id)
            if child_id not in tasks[parent_id].setdefault("children", []):
                tasks[parent_id]["children"].append(child_id)
            tasks[child_id]["status"] = (
                "ready"
                if all(
                    tasks.get(item, {}).get("status") == "done"
                    for item in tasks[child_id]["parents"]
                )
                else "todo"
            )
            return {"parent": tasks[parent_id], "child": tasks[child_id]}

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            link,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_link
