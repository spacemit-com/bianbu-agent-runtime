import json
from bianbu_agent.tools import tool
from .client import run


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def browser_press(key: str):
        """Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.

        Args:
            key: Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')"""
        arguments = {
            key: value for key, value in {"key": key}.items() if value is not None
        }
        payload = await run(package_context.call.session_id, "press", (str(arguments.get("key", "")),))
        return json.dumps(payload, ensure_ascii=False)

    return browser_press
