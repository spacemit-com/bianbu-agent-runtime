import json
from bianbu_agent.tools import tool
from .client import run


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def browser_back():
        """Navigate back to the previous page in browser history. Requires browser_navigate to be called first."""
        return json.dumps(await run(package_context.call.session_id, "back"), ensure_ascii=False)

    return browser_back
