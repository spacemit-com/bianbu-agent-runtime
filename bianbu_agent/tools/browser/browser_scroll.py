import json
from bianbu_agent.tools import tool
from .client import run


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def browser_scroll(direction: str):
        """Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.

        Args:
            direction: Direction to scroll"""
        arguments = {
            key: value
            for key, value in {"direction": direction}.items()
            if value is not None
        }
        direction = arguments.get("direction")
        if direction not in {"up", "down"}:
            return json.dumps(
                {
                    "success": False,
                    "code": "invalid_direction",
                    "error": "direction must be up or down",
                }
            )
        return json.dumps(
            await run(package_context.call.session_id, "scroll", (direction, "500")), ensure_ascii=False
        )

    return browser_scroll
