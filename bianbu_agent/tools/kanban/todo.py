from __future__ import annotations
import json
from bianbu_agent.tools import tool

_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})


def _normalize(item):
    if not isinstance(item, dict):
        return {"id": "?", "content": "(invalid item)", "status": "pending"}
    status = str(item.get("status", "pending")).strip().lower()
    return {
        "id": str(item.get("id", "?")).strip() or "?",
        "content": str(item.get("content", "(no description)")).strip()[:4000]
        or "(no description)",
        "status": status if status in _STATUSES else "pending",
    }


def _manage(todos):
    last = {}
    for position, raw in enumerate(todos):
        item = _normalize(raw)
        last[item["id"]] = (position, item)
    items = [item for _, item in sorted(last.values())][:256]
    summary = {
        status: sum(
            (item.get("status") == status for item in items if isinstance(item, dict))
        )
        for status in _STATUSES
    }
    summary["total"] = len(items)
    return {"todos": items, "summary": summary}


def create_tool(_package_context):

    @tool(parse_docstring=True)
    async def todo(todos: list[object]):
        """Publish your complete task list for the current session. Use for complex tasks with 3+ steps or when the user provides multiple tasks.

        Provide every current item on each call. The returned list is captured in
        the session's tool events; this tool keeps no separate persistent state.

        Each item: {id: string, content: string, status: pending|in_progress|completed|cancelled}
        List order is priority. Only ONE item in_progress at a time.
        Mark items completed immediately when done. If something fails, cancel it and add a revised item.

        Always returns the full current list.

        Args:
            todos: Complete current task list."""
        return json.dumps(_manage(todos), ensure_ascii=False)

    return todo
