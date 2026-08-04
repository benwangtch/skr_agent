#!/usr/bin/env python3
"""End-to-end run of the report agent against the fixture data.

    uv run python examples/run_report.py                  # user-triggered sweep, own division
    uv run python examples/run_report.py --scheduled       # service-account sweep, exec roll-up
    uv run python examples/run_report.py --ask "..."       # ad-hoc question via copilot
    uv run python examples/run_report.py --dry-run         # research only, no publish
    uv run python examples/run_report.py --reader-only     # exercise the refusal path

Needs credentials for the Claude Agent SDK (ANTHROPIC_API_KEY, or an
`ant auth login` profile).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from skr_agent import Budget, build_copilot, build_mesh
from skr_agent.principals import service_principal, user_principal
from skr_agent.protocol import AgentRequest

ROOT = Path(__file__).resolve().parent.parent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask", help="Route a question through copilot instead.")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Run as the weekly service account (cross-division, publishes to exec).",
    )
    parser.add_argument("--division", default="supply", help="Division for a user-triggered run.")
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

    if args.scheduled:
        principal = service_principal()
    else:
        roles = {"wiki.reader"} | (set() if args.reader_only else {"wiki.writer"})
        principal = user_principal(
            f"demo.user@{args.division}", args.division, roles=roles, token="demo-token"
        )

    if args.ask:
        agent = build_copilot(mesh.registry, wiki_backend=mesh.backend, wiki_authz=mesh.authz)
        request = AgentRequest(principal=principal, task=args.ask, budget=Budget(max_turns=20))
    else:
        agent = mesh.report_agent
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
        print(f"\nwiki namespaces now: {', '.join(mesh.backend.list_namespaces())}")


if __name__ == "__main__":
    asyncio.run(main())
