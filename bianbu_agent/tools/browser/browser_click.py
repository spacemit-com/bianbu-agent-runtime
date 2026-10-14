import json
from bianbu_agent.tools import tool
from .client import run


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def browser_click(ref: str):
        """Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.

        Args:
            ref: The element reference from the snapshot (e.g., '@e5', '@e12')"""
        arguments = {
            key: value for key, value in {"ref": ref}.items() if value is not None
        }
        ref = str(arguments.get("ref", ""))
        if not ref.startswith("@"):
            ref = "@" + ref
        return json.dumps(
            await run(package_context.call.session_id, "click", (ref,)), ensure_ascii=False
        )

    return browser_click
