import json
from bianbu_agent.tools import tool
from .client import run


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def browser_console(clear: bool = False, expression: str = None):
        """Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first. When 'expression' is provided, evaluates JavaScript in the page context and returns the result — use this for DOM inspection, reading page state, or extracting data programmatically.

        Args:
            clear: If true, clear the message buffers after reading
            expression: JavaScript expression to evaluate in the page context. Runs in the browser like DevTools console — full access to DOM, window, document. Return values are serialized to JSON. Example: 'document.title' or 'document.querySelectorAll("a").length'
        """
        arguments = {
            key: value
            for key, value in {"clear": clear, "expression": expression}.items()
            if value is not None
        }
        expression = arguments.get("expression")
        if expression is not None:
            command, args = ("eval", (str(expression),))
        else:
            command, args = ("console", ("--clear",) if arguments.get("clear") else ())
        return json.dumps(await run(package_context.call.session_id, command, args), ensure_ascii=False)

    return browser_console
