import json
from datetime import datetime, timezone
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_create(
        assignee: str,
        title: str,
        board: str = None,
        body: str = None,
        idempotency_key: str = None,
        parents: list[object] = None,
        priority: int = None,
        project: str = None,
        tenant: str = None,
        triage: bool = None,
    ):
        """创建持久化 Kanban 任务，可指定负责人标签、父任务、优先级和幂等键。就绪任务会由后台 Dispatcher 按负责人启动独立 Agent worker。

        Args:
            assignee: 任务的 Agent 角色标签（必填）；后台 Dispatcher 会按该角色启动独立 worker。
            title: Short task title (required).
            board: 可选看板标识；省略时使用默认看板。
            body: Opening post: full spec, acceptance criteria, links. The assigned worker reads this as part of its context.
            idempotency_key: If a non-archived task with this key already exists, return that task's id instead of creating a duplicate. Useful for retry-safe automation.
            parents: Parent task ids. The new task stays in 'todo' until every parent reaches 'done'; then it auto-promotes to 'ready'. Typical fan-in: list all the researcher task ids when creating a synthesizer task.
            priority: Dispatcher tiebreaker. Higher = picked sooner when multiple ready tasks share an assignee.
            project: Optional project id or slug to link the task to. When set, the task becomes a git worktree under the project's primary repo with a deterministic branch (project slug + task id), instead of a random branch.
            tenant: Optional namespace for multi-project isolation. Defaults to BIANBU_AGENT_TENANT env if set.
            triage: If true, task lands in 'triage' instead of 'todo' — a specifier profile is expected to flesh out the body before work starts.
        """
        arguments = {
            key: value
            for key, value in {
                "assignee": assignee,
                "title": title,
                "board": board,
                "body": body,
                "idempotency_key": idempotency_key,
                "parents": parents,
                "priority": priority,
                "project": project,
                "tenant": tenant,
                "triage": triage,
            }.items()
            if value is not None
        }

        def create(state):
            state.setdefault("tasks", {})
            parents = list(
                dict.fromkeys(str(item) for item in arguments.get("parents") or [])
            )
            missing = [
                parent_id for parent_id in parents if parent_id not in state["tasks"]
            ]
            if missing:
                return {
                    "success": False,
                    "code": "invalid_parent",
                    "error": "Every parent Kanban task must exist on the same board",
                    "missing_parent_ids": missing,
                }
            key = arguments.get("idempotency_key")
            if key:
                for existing in state["tasks"].values():
                    if (
                        existing.get("idempotency_key") == key
                        and existing.get("status") != "archived"
                    ):
                        return {"task": existing, "created": False}
            state["counter"] = int(state.get("counter", 0)) + 1
            task_id = f"k-{state['counter']:06d}"
            task = {
                "id": task_id,
                "title": str(arguments.get("title", "")),
                "assignee": str(arguments.get("assignee", "")),
                "body": str(arguments.get("body", "")),
                "status": (
                    "triage"
                    if arguments.get("triage")
                    else (
                        "ready"
                        if all(
                            state["tasks"][parent]["status"] == "done"
                            for parent in parents
                        )
                        else "todo"
                    )
                ),
                "parents": parents,
                "children": [],
                "comments": [],
                "attachments": [],
                "priority": int(arguments.get("priority", 0)),
                "tenant": arguments.get("tenant"),
                "project": arguments.get("project"),
                "board": state["board"],
                "idempotency_key": key,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            state["tasks"][task_id] = task
            for parent_id in parents:
                children = state["tasks"][parent_id].setdefault("children", [])
                if task_id not in children:
                    children.append(task_id)
            return {"task": task, "created": True}

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            create,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_create
