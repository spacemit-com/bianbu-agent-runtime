import json
import urllib.parse
from bianbu_agent.tools import tool
from .client import run


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def browser_navigate(url: str):
        """Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer web_search or web_extract (faster, cheaper). For plain-text endpoints — URLs ending in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any documented API endpoint — prefer curl via the terminal tool or web_extract; the browser stack is overkill and much slower for these. Use browser tools when you need to interact with a page (click, fill forms, dynamic content). Returns a compact page snapshot with interactive elements and ref IDs — no need to call browser_snapshot separately after navigating.

        Args:
            url: The URL to navigate to (e.g., 'https://example.com')"""
        arguments = {
            key: value for key, value in {"url": url}.items() if value is not None
        }
        url = arguments.get("url")
        parsed = urllib.parse.urlparse(str(url or ""))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            return json.dumps(
                {
                    "success": False,
                    "code": "invalid_url",
                    "error": "url must be an HTTP(S) URL without embedded credentials",
                }
            )
        return json.dumps(
            await run(package_context.call.session_id, "open", (str(url),)), ensure_ascii=False
        )

    return browser_navigate
