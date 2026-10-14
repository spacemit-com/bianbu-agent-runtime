import json
from bianbu_agent.tools import tool
from .client import run


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def browser_type(ref: str, text: str):
        """Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.

        Args:
            ref: The element reference from the snapshot (e.g., '@e3')
            text: The text to type into the field"""
        arguments = {
            key: value
            for key, value in {"ref": ref, "text": text}.items()
            if value is not None
        }
        ref = str(arguments.get("ref", ""))
        if not ref.startswith("@"):
            ref = "@" + ref
        payload = await run(
            package_context.call.session_id,
            "fill", (ref, str(arguments.get("text", "")))
        )
        return json.dumps(payload, ensure_ascii=False)

    return browser_type
