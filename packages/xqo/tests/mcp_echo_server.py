"""Tiny stdio MCP server used by the tests."""
try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

app = Server("echo")


@app.tool()
def echo(text: str) -> str:
    """Echo text back in upper case."""
    return text.upper()


@app.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    app.run()
