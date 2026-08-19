"""MCP server entry point.

The shell tools described in the RFC will be registered here as they are
implemented and validated.
"""

from mcp.server.fastmcp import FastMCP

server = FastMCP("tfbash-mcp")


def main() -> None:
    """Run the MCP server over the standard input/output transport."""
    server.run(transport="stdio")
