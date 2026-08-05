"""Run the A2A server and the scheduler together in one process.

This is what "被 serve 起來，且同時可以執行 cronjob" means concretely: one
``asyncio`` event loop, one process, holding a live ``uvicorn`` server for
inbound A2A calls and a ``Scheduler`` polling loop for outbound scheduled
runs. Both hit the same mesh (same wiki backend, same principals module), so
a scheduled sweep and an A2A-triggered one are the same code path with a
different trigger.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uvicorn

from ..assembly import build_mesh
from ..principals import service_principal
from ..protocol import Budget
from .a2a import build_a2a_app
from .scheduler import ScheduledJob, Scheduler

log = logging.getLogger(__name__)

__all__ = ["default_jobs", "run"]


def default_jobs(mesh, *, cron: str = "0 8 * * 1") -> list[ScheduledJob]:
    """The one scheduled job this repo ships out of the box: a weekly,
    all-tiers BOM sweep published as the executive roll-up.

    ``cron`` defaults to Monday 08:00 UTC — override per deployment; this is
    a starting point, not a policy.
    """
    return [
        ScheduledJob(
            name="weekly-bom-sweep",
            cron=cron,
            agent=mesh.report_agent,
            task=(
                "Run the weekly supply-chain incident sweep over the full bill "
                "of materials and publish the report to the wiki."
            ),
            inputs={"publish": True},
            principal=service_principal,  # called fresh each firing
            budget=Budget(max_turns=80),
        )
    ]


async def run(
    *,
    fixtures: str | Path,
    project_root: str | Path,
    host: str = "0.0.0.0",
    port: int = 8000,
    cron: str = "0 8 * * 1",
    scheduler_poll_interval: float = 30.0,
) -> None:
    """Build the mesh, then run the A2A server and the scheduler concurrently
    until either one stops (normally: never — this is a foreground service)."""
    mesh = build_mesh(fixtures=fixtures, project_root=project_root)

    base_url = f"http://{host}:{port}/"
    a2a_app = build_a2a_app(mesh.report_agent, url=base_url, registry=mesh.registry)
    server = uvicorn.Server(
        uvicorn.Config(a2a_app.build(), host=host, port=port, log_level="info")
    )

    scheduler = Scheduler(default_jobs(mesh, cron=cron))

    log.info("serving %s at %s (A2A) + scheduler poll=%ss", mesh.report_agent.name, base_url, scheduler_poll_interval)
    await asyncio.gather(
        server.serve(),
        scheduler.run_forever(poll_interval=scheduler_poll_interval),
    )
