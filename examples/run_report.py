#!/usr/bin/env python3
"""End-to-end run of the report agent against the fixture data.

    python examples/run_report.py                  # full BOM sweep, publishes
    python examples/run_report.py --ask "..."      # ad-hoc question via copilot
    python examples/run_report.py --dry-run        # research only, no publish

Needs credentials for the Claude Agent SDK (ANTHROPIC_API_KEY, or an
`ant auth login` profile).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from skr_agent import Budget, Principal, build_copilot, build_mesh
from skr_agent.protocol import AgentRequest

ROOT = Path(__file__).resolve().parent.parent


def make_principal(division: str, *, writer: bool) -> Principal:
    roles = {"wiki.reader"} | ({"wiki.writer"} if writer else set())
    return Principal(
        subject=f"demo.user@{division}",
        division=division,
        roles=frozenset(roles),
        token="demo-token",
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask", help="Route a question through copilot instead.")
    parser.add_argument("--division", default="supply")
    parser.add_argument("--tier", default="critical")
    parser.add_argument("--dry-run", action="store_true", help="Do not publish.")
    parser.add_argument("--reader-only", action="store_true", help="Drop the write role.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    mesh = build_mesh(fixtures=ROOT / "fixtures", project_root=ROOT)
    principal = make_principal(args.division, writer=not args.reader_only)

    if args.ask:
        agent = build_copilot(mesh.registry)
        request = AgentRequest(
            principal=principal,
            task=args.ask,
            budget=Budget(max_turns=20),
        )
    else:
        agent = mesh.report_agent
        request = AgentRequest(
            principal=principal,
            task=(
                f"Run the weekly supply-chain incident sweep over the {args.tier} "
                f"tier of the BOM and produce the report."
                + ("" if args.dry_run else " Publish it to the wiki when done.")
            ),
            inputs={"tier": args.tier, "publish": not args.dry_run},
            budget=Budget(max_turns=60),
        )

    print(f"→ {agent.name}: {request.task}\n")
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

    if not args.dry_run and not args.ask:
        pages = mesh.wiki.backend.list_namespaces()
        print(f"\nwiki namespaces now: {', '.join(pages)}")


if __name__ == "__main__":
    asyncio.run(main())
