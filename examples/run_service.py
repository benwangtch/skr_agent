#!/usr/bin/env python3
"""Serve the report agent over A2A and run its scheduled sweep, together.

    uv run python examples/run_service.py                       # port 8000, Monday 08:00 UTC
    uv run python examples/run_service.py --port 8080 --cron "*/5 * * * *"   # every 5 min, for testing

Once running:
    curl http://localhost:8000/.well-known/agent-card.json | jq

Sending it a task needs an A2A client (a2a-sdk ships one) or a raw JSON-RPC
POST to /:

    curl http://localhost:8000/ -H 'Content-Type: application/json' \\
      -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{
            "message":{"messageId":"m1","role":"user",
                       "parts":[{"kind":"text","text":"our ASC-4400 exposure?"}]}}}'

Use "method":"message/stream" instead to receive progress events over SSE as
the agent works, rather than waiting for the finished report.

Needs LLM credentials for whichever LLM_PROVIDER is configured — see
.env.example. The A2A/scheduler wiring itself needs no credentials to start;
it only builds a model client once a task actually arrives or a cron job
fires.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from deep_research_agent.serving import run

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cron", default="0 8 * * 1", help="Weekly sweep schedule (UTC).")
    parser.add_argument("--poll-interval", type=float, default=30.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    asyncio.run(
        run(
            fixtures=ROOT / "fixtures",
            project_root=ROOT,
            host=args.host,
            port=args.port,
            cron=args.cron,
            scheduler_poll_interval=args.poll_interval,
        )
    )


if __name__ == "__main__":
    main()
