#!/usr/bin/env python3
"""Ask the agent anything — the face-to-user path, with no domain configured.

    uv run python examples/ask.py "Why did our Q3 lead times slip?"
    uv run python examples/ask.py --division platform "What do we know about the auth rewrite?"
    uv run python examples/ask.py --read-only "..."     # research, never publish

This is the other half of the deployment story from `run_report.py`. That one
runs a *known* task on a schedule, so it loads the supply-chain domain: BOM
sources, an investigator that understands aliases, a severity rubric.

Here the question is whatever the user typed. There is no domain to select in
advance, so none is loaded (`domain=None`) — the agent gets the generic
research loop, the internal wiki, and any MCP servers you configured, and
works out the rest itself. The research machinery is identical either way:
same planning, same scratchpad, same fact-checker before anything publishes.

Needs LLM credentials — set LLM_API_KEY in .env (see .env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from deep_research_agent import Budget, build_mesh
from deep_research_agent.mcp import mcp_toolset_from_config
from deep_research_agent.observability import flush
from deep_research_agent.principals import user_principal
from deep_research_agent.protocol import AgentRequest

ROOT = Path(__file__).resolve().parent.parent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", help="Anything. It plans its own investigation.")
    parser.add_argument("--division", default="supply", help="Whose scope to run under.")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Drop the write role, so it researches and answers without publishing.",
    )
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    mcp_toolset = await mcp_toolset_from_config()
    if mcp_toolset:
        print("→ MCP tools loaded from configured server(s)")

    mesh = build_mesh(
        fixtures=ROOT / "fixtures",
        project_root=ROOT,
        domain=None,  # the whole point of this example
        extra_toolsets=[mcp_toolset] if mcp_toolset else (),
    )

    roles = {"wiki.reader"} | (set() if args.read_only else {"wiki.writer"})
    principal = user_principal(
        f"demo.user@{args.division}", args.division, roles=roles, token="demo-token"
    )

    request = AgentRequest(
        principal=principal, task=args.question, budget=Budget(max_turns=args.max_turns)
    )

    print(f"→ {mesh.agent.name} (no domain) as {principal.subject}")
    print(f"  {args.question}\n")

    # Streamed rather than awaited: an open-ended question can run for minutes,
    # and a user who typed it is sitting there watching.
    response = None
    async for event in mesh.agent.stream(request):
        if isinstance(event, tuple):
            print(f"  · {event[1]}")
        else:
            response = event

    assert response is not None
    print(f"\n=== {response.status} (trace {response.trace_id}) ===\n")
    print(response.output or "(no output)")

    if response.citations:
        print("\n--- citations ---")
        for c in response.citations:
            print(f"  [{c.kind}] {c.title or c.ref} — {c.ref}")

    u = response.usage
    print(f"\n--- usage --- turns={u.turns} in={u.input_tokens} out={u.output_tokens}")

    # Short-lived process: the tracing SDK batches in the background, so
    # without this the run's trace can be discarded at exit.
    flush()


if __name__ == "__main__":
    asyncio.run(main())
