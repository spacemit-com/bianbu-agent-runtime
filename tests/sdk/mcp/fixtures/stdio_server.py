from mcp.server.fastmcp import FastMCP


mcp = FastMCP("Bianbu Agent MCP integration fixture")


@mcp.tool()
def echo(message: str) -> str:
    """Return the supplied message."""

    return message


if __name__ == "__main__":
    mcp.run(transport="stdio")
