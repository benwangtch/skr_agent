"""A real MCP server, run as a subprocess by ``tests/test_mcp.py``.

Deliberately a real server over the real stdio transport rather than a mock:
the thing worth testing is that this codebase's assumptions about
``langchain-mcp-adapters`` hold — that tools arrive as ``BaseTool``s with a
usable schema, and that each invocation opens its own session. A mock would
assert those assumptions rather than check them.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("supplier-risk")


@mcp.tool()
def get_supplier_risk_score(supplier_id: str) -> str:
    """Return the internal risk score for one supplier."""
    return f"{supplier_id}: risk=amber, last_audit=2026-03-11"


@mcp.tool()
def list_open_audits(division: str = "all") -> str:
    """List open supplier audits, optionally for one division."""
    return f"2 open audits for {division}: acme-semi (Q2), nordwind (Q3)"


if __name__ == "__main__":
    mcp.run(transport="stdio")
