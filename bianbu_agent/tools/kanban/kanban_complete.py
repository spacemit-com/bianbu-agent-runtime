import json
from datetime import datetime, timezone
import asyncio
from bianbu_agent.tools import mutate_kanban, tool


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def kanban_complete(
        task_id: str,
        artifacts: list[object] = None,
        board: str = None,
        created_cards: list[object] = None,
        metadata: dict[str, object] = None,
        summary: str = None,
    ):
        """将 Kanban 任务标记为完成，并保存结构化交接摘要和产物信息。

        Args:
            task_id: 要操作的 Kanban 任务 ID（必填）。
            artifacts: Optional list of absolute paths to deliverable files you produced during this run — generated charts, PDFs, spreadsheets, images, archives. Examples: ["/tmp/q3-revenue.png", "/tmp/report.pdf"]. The gateway notifier uploads each path as a native attachment to the subscribed chat (images embed inline, everything else uploads as a file) so the deliverable lands with the completion notification. Skip intermediate scratch files and references that are not the deliverable. The path must exist on disk at completion. Files inside a managed scratch workspace are copied to durable task attachments before cleanup; a missing declared scratch artifact keeps the task in-flight so you can fix the path and retry.
            board: 可选看板标识；省略时使用默认看板。
            created_cards: Optional structured manifest of task ids you created via ``kanban_create`` during this run. The kernel verifies each id exists and was created by this worker's profile; any phantom id blocks the completion with an error listing what went wrong (auditable in the task's events). Only list ids you got back from a successful ``kanban_create`` call — do not invent or remember ids from prose. Omit the field if you did not create any cards.
            metadata: Free-form dict of structured facts about this attempt — {"changed_files": [...], "tests_run": 12, "findings": [...]}. Surfaced to downstream workers alongside ``summary``.
            summary: Human-readable handoff, 1-3 sentences. Appears in Run History on the dashboard and in downstream workers' context.
        """
        arguments = {
            key: value
            for key, value in {
                "task_id": task_id,
                "artifacts": artifacts,
                "board": board,
                "created_cards": created_cards,
                "metadata": metadata,
                "summary": summary,
            }.items()
            if value is not None
        }

        def complete(state):
            tasks = state.setdefault("tasks", {})
            task = tasks.get(str(arguments.get("task_id", "")))
            if not task:
                return {
                    "success": False,
                    "code": "not_found",
                    "error": "Kanban task not found",
                }
            task.update(
                status="done",
                summary=str(arguments.get("summary") or ""),
                metadata=arguments.get("metadata") or {},
                artifacts=list(arguments.get("artifacts") or []),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            for child_id in task.get("children", []):
                child = tasks.get(child_id)
                if child and all(
                    (
                        tasks.get(parent, {}).get("status") == "done"
                        for parent in child.get("parents", [])
                    )
                ):
                    child["status"] = "ready"
            return {"task": task}

        payload = await asyncio.to_thread(
            mutate_kanban,
            package_context.workspace_root,
            arguments.get("board"),
            complete,
        )
        return json.dumps(payload, ensure_ascii=False)

    return kanban_complete
