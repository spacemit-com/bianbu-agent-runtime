from bianbu_agent.tools import tool
from .web import extract_web_content


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def web_extract(urls: list[object], char_limit: int = None):
        """Bianbu Agent tool: web_extract

        Args:
            urls: Public HTTP(S) URLs to extract.
            char_limit: Maximum extracted characters per page."""
        arguments = {
            key: value
            for key, value in {"urls": urls, "char_limit": char_limit}.items()
            if value is not None
        }
        value = await extract_web_content(dict(arguments))
        return str(value)

    return web_extract
