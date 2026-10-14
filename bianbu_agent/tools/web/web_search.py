import asyncio
from typing import Literal

from bianbu_agent.tools import tool
from .search import search_web


_SEARXNG_URL = "http://127.0.0.1:8888"


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def web_search(
        query: str, limit: int = 5,
        category: Literal["general", "news"] = "general",
        time_range: Literal["", "day", "month", "year"] = "",
    ):
        """Search the web; use category=news for current events.

        Published dates are search-index metadata. Verify publication/event dates
        and claims with web_extract before using them in a news report; if source
        pages are unavailable, label unverified details rather than inventing them.

        Args:
            query: Search query.
            limit: Maximum number of normalized search results to return.
            category: General web or news search.
            time_range: Optional engine time filter: day, month, year, or empty for unrestricted."""
        arguments = {
            key: value
            for key, value in {"query": query, "limit": limit, "category": category, "time_range": time_range}.items()
            if value is not None
        }
        value = await asyncio.to_thread(
            search_web, dict(arguments), base_url=_SEARXNG_URL
        )
        return str(value)

    return web_search
