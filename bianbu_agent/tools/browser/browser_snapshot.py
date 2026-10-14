import json
from bianbu_agent.tools import tool
from .client import run


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def browser_snapshot(full: bool = False):
        """Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. Snapshots over 15000 chars are truncated or LLM-summarized; when that happens the complete snapshot is saved to a file and the output includes its path so you can page through the rest with read_file. Requires browser_navigate first. Note: browser_navigate already returns a compact snapshot — use this to refresh after interactions that change the page, or with full=true for complete content.

        Args:
            full: If true, returns complete page content. If false (default), returns compact view with interactive elements only.
        """
        arguments = {
            key: value for key, value in {"full": full}.items() if value is not None
        }
        args = () if arguments.get("full") else ("-c",)
        return json.dumps(
            await run(package_context.call.session_id, "snapshot", args), ensure_ascii=False
        )

    return browser_snapshot
