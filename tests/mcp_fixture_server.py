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


@mcp.tool()
def search_wiki_pages(query: str, top_k: int = 5) -> dict:
    """Search an internal wiki. Shaped like the real AI4BI tool of this name.

    Returned as a dict so the test exercises the structured-content path, and
    with the same envelope the real tool uses -- a `hits` list whose entries
    carry namespace and page_name -- because what is being tested is this
    codebase's assumption about that shape.
    """
    return {
        "query": query,
        "namespaces_searched": ["supply"],
        "total_returned": 2,
        "truncated": False,
        "hits": [
            {
                "page_id": 41,
                "page_name": "acme-semiconductor",
                "namespace": "supply",
                "description": "Supplier profile for Acme Semiconductor.",
                "score": 8.5,
                "content": "Acme runs two fabs.",
            },
            {
                "page_id": 42,
                "page_name": "nordwind-logistics",
                "namespace": "supply",
                "description": "Freight partner.",
                "score": 3.1,
                "content": "",
            },
        ],
        "note": None,
    }


@mcp.tool()
def search_nothing_useful(query: str) -> dict:
    """A wiki-ish tool whose envelope does not match. Exists to prove the
    mismatch is reported rather than silently producing no documents."""
    return {"query": query, "results": []}


if __name__ == "__main__":
    mcp.run(transport="stdio")
