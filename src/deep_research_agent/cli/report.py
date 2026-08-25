"""The supply-chain sweep — a known task, which is what the schedule runs.

    python -m deep_research_agent report                  # user-triggered sweep, own division
    python -m deep_research_agent report --scheduled      # service-account sweep, exec roll-up
    python -m deep_research_agent report --ask "..."      # ad-hoc question, domain still loaded
    python -m deep_research_agent report --dry-run        # research only, no publish
    python -m deep_research_agent report --reader-only    # exercise the refusal path

The supply-chain domain is loaded here, which is the difference from ``ask``:
the task is known in advance, so it is worth encoding what a BOM company is
and why aliases matter. For an arbitrary question, use ``ask``.

Needs LLM credentials — set LLM_API_KEY in .env (see .env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from deep_research_agent import Budget, build_mesh
from deep_research_agent.config import get_paths
from deep_research_agent.mcp import mcp_toolset_from_config
from deep_research_agent.observability import flush
from deep_research_agent.principals import service_principal, user_principal
from deep_research_agent.protocol import AgentRequest


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m deep_research_agent report",
        description="Run the supply-chain sweep. The task the schedule runs.",
    )
    p.add_argument("--ask", help="Ask one ad-hoc question instead of running a sweep.")
    p.add_argument(
        "--scheduled",
        action="store_true",
        help="Run as the weekly service account (cross-division, publishes to exec).",
    )
    p.add_argument("--division", default="supply", help="Division for a user-triggered run.")
    p.add_argument("--tier", default="critical")
    p.add_argument("--dry-run", action="store_true", help="Do not publish.")
    p.add_argument("--reader-only", action="store_true", help="Drop the write role.")
    p.add_argument(
        "--out",
        metavar="PATH",
        help="Write the published report to this file (markdown). "
        "Without it the report is printed but lost when the process exits, "
        "because the fixture wiki lives in memory.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def run(args: argparse.Namespace) -> int:
    paths = get_paths()

    mcp_toolset = await mcp_toolset_from_config()
    if mcp_toolset:
        print("→ MCP tools loaded from configured server(s)")
    mesh = build_mesh(
        fixtures=paths.resolved_fixtures(),
        project_root=paths.resolved_project_root(),
        extra_toolsets=[mcp_toolset] if mcp_toolset else (),
    )

    if args.scheduled:
        principal = service_principal()
    else:
        roles = {"wiki.reader"} | (set() if args.reader_only else {"wiki.writer"})
        principal = user_principal(
            f"demo.user@{args.division}", args.division, roles=roles, token="demo-token"
        )

    agent = mesh.agent

    if args.ask:
        request = AgentRequest(principal=principal, task=args.ask, budget=Budget(max_turns=20))
    else:
        scope = "the full bill of materials" if args.scheduled else f"the {args.tier} tier"
        request = AgentRequest(
            principal=principal,
            task=(
                f"Run a supply-chain incident sweep over {scope} and produce the report."
                + ("" if args.dry_run else " Publish it to the wiki when done.")
            ),
            inputs={"tier": args.tier, "publish": not args.dry_run},
            budget=Budget(max_turns=60),
        )

    print(f"→ {agent.name} as {principal.subject} ({', '.join(sorted(principal.roles))})")
    print(f"  {request.task}\n")

    # Snapshot so we can tell which pages this run actually wrote.
    before = mesh.backend.page_refs()
    response = await agent.run(request)

    print(f"\n=== {response.status} (trace {response.trace_id}) ===\n")
    print(response.output or "(no output)")

    if response.citations:
        print("\n--- citations ---")
        for c in response.citations:
            print(f"  [{c.kind}] {c.title or c.ref} — {c.ref}")

    u = response.usage
    print(
        f"\n--- usage --- turns={u.turns} in={u.input_tokens} "
        f"out={u.output_tokens} cost=${u.cost_usd:.4f}"
    )

    # The agent's final message is deliberately a short summary -- the report
    # itself is the wiki page it published. Print that, or it is invisible.
    published = sorted(mesh.backend.page_refs() - before)
    if published:
        pages = [mesh.backend.get(ref) for ref in published]
        rendered = "\n\n---\n\n".join(
            f"<!-- {page.ref} -->\n# {page.title}\n\n{page.body}\n\n"
            f"**Sources:** {', '.join(page.source_refs) or 'none'}"
            for page in pages
            if page is not None
        )
        print(f"\n=== published report ({', '.join(published)}) ===\n")
        print(rendered)
        if args.out:
            Path(args.out).write_text(rendered, encoding="utf-8")
            print(f"\n(written to {args.out})")
    elif not args.dry_run and not args.ask:
        print("\nNo page was published. Either the agent chose not to, or the "
              "write was refused -- run with -v and look for 'wiki.write'.")

    flush()
    return 0 if response.status == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
