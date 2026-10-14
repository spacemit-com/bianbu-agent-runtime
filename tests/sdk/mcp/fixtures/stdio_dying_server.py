"""Stdio MCP fixture whose child can be killed from the client side."""

import os

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("Bianbu Agent MCP dying fixture")


@mcp.tool()
def echo(message: str) -> str:
    """Return the supplied message."""

    return message


@mcp.tool()
def exit_process(count_file: str) -> str:
    """Record this call in count_file, then exit without answering it."""

    with open(count_file, "a", encoding="utf-8") as stream:
        stream.write("ran\n")
    os._exit(3)


if __name__ == "__main__":
    mcp.run(transport="stdio")
