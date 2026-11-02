"""Authorization signatures shared without importing the MCP SDK.

Plugin discovery and the Plugin manager only need the type alias.  Importing
``bianbu_agent.mcp.plugin`` would pull the ``mcp`` SDK, which costs roughly
0.6s of every Worker cold start even though a Worker starts MCP servers only
after it has a prompt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

MCPAuthorizer = Callable[[str, str], bool | Awaitable[bool]]

__all__ = ["MCPAuthorizer"]
