"""Serve the agent over A2A and run its scheduled sweep, together.

    python -m deep_research_agent serve                       # port 8000, Monday 08:00 UTC
    python -m deep_research_agent serve --port 8080 --cron "*/5 * * * *"   # every 5 min, for testing

Once running:
    curl http://localhost:8000/.well-known/agent-card.json | jq

Sending it a task needs an A2A client (a2a-sdk ships one) or a raw JSON-RPC
POST to /. The A2A-Version header and the body shape go together — omit the
header and the SDK reads the request as A2A 0.3:

    curl http://localhost:8000/ \\
      -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \\
      -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{
            "message":{"messageId":"m1","role":"ROLE_USER",
                       "parts":[{"text":"our ASC-4400 exposure?"}]}}}'

Use "SendStreamingMessage" instead to receive progress events over SSE as the
agent works, rather than waiting for the finished report.

Needs LLM credentials for whichever LLM_PROVIDER is configured — see
.env.example. The A2A/scheduler wiring itself needs no credentials to start;
it only builds a model client once a task actually arrives or a cron job
fires.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from deep_research_agent.config import get_paths
from deep_research_agent.serving import run


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m deep_research_agent serve",
        description="A2A server and scheduler in one process.",
    )
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--cron", default="0 8 * * 1", help="Weekly sweep schedule (UTC).")
    p.add_argument("--poll-interval", type=float, default=30.0)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    paths = get_paths()
    asyncio.run(
        run(
            fixtures=paths.resolved_fixtures(),
            project_root=paths.resolved_project_root(),
            host=args.host,
            port=args.port,
            cron=args.cron,
            scheduler_poll_interval=args.poll_interval,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
