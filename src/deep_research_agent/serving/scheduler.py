"""A cron-like scheduler for running a skill on a recurring schedule.

Deliberately not Claude Managed Agents' scheduled deployments (see the
`claude-api` skill's ``managed-agents-scheduled-deployments`` docs) — this
runs in *this* process, against *this* mesh, alongside the A2A server, so a
deployment doesn't need a second hosted platform just to fire the weekly BOM
sweep. If a job's work outgrows a single process (needs its own scaling,
needs to survive this process restarting mid-run), that's the point at which
Managed Agents' deployments become the better fit — this module intentionally
doesn't try to be that.

Every job runs under a ``Principal`` the caller supplies, usually
``service_principal()`` — that's what gives a scheduled sweep the
cross-division read and exec-namespace write access a user-triggered run
doesn't get. See ``docs/design/DESIGN.md`` §5.3 and ``principals.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from croniter import croniter

from deep_research_agent.protocol import AgentRequest, AgentResponse, Budget, Principal
from deep_research_agent.runtime import DeepAgent

log = logging.getLogger(__name__)

__all__ = ["ScheduledJob", "Scheduler"]

ResultHook = Callable[["ScheduledJob", "AgentResponse | BaseException"], Awaitable[None] | None]


@dataclass
class ScheduledJob:
    """One recurring skill run: an agent, a task, a schedule, and who runs it.

    ``task`` / ``inputs`` / ``principal`` accept either a fixed value or a
    zero-arg callable — a callable ``principal`` is how a job mints a fresh
    service credential per firing instead of holding one for the process's
    lifetime.
    """

    name: str
    cron: str
    """Standard 5-field cron expression, e.g. ``"0 8 * * 1"`` (Monday 08:00)."""
    agent: DeepAgent
    task: str | Callable[[], str]
    principal: Principal | Callable[[], Principal]
    inputs: dict[str, Any] | Callable[[], dict[str, Any]] = field(default_factory=dict)
    budget: Budget = field(default_factory=lambda: Budget(max_turns=60))
    timezone: dt.tzinfo = dt.timezone.utc

    def __post_init__(self) -> None:
        if not croniter.is_valid(self.cron):
            raise ValueError(f"job {self.name!r}: invalid cron expression {self.cron!r}")

    def next_fire(self, after: dt.datetime) -> dt.datetime:
        return croniter(self.cron, after).get_next(dt.datetime)

    def _resolve(self, value: Any) -> Any:
        return value() if callable(value) else value

    def build_request(self) -> AgentRequest:
        return AgentRequest(
            principal=self._resolve(self.principal),
            task=self._resolve(self.task),
            inputs=self._resolve(self.inputs) or {},
            budget=self.budget,
        )


class Scheduler:
    """Runs a fixed list of ``ScheduledJob``s against their cron schedules."""

    def __init__(self, jobs: list[ScheduledJob], *, on_result: ResultHook | None = None) -> None:
        self.jobs = jobs
        self.on_result = on_result
        self._next: dict[str, dt.datetime] = {}

    def _seed(self, now: dt.datetime) -> None:
        for job in self.jobs:
            if job.name not in self._next:
                self._next[job.name] = job.next_fire(now)

    def due_jobs(self, now: dt.datetime) -> list[ScheduledJob]:
        """Jobs whose scheduled time has arrived, without advancing state.

        Pure with respect to `self._next` — safe to call repeatedly from a
        test without side effects. ``run_forever`` is the only caller that
        also advances the schedule (via ``run_job``).
        """
        self._seed(now)
        return [job for job in self.jobs if self._next[job.name] <= now]

    async def run_job(self, job: ScheduledJob, *, now: dt.datetime | None = None) -> AgentResponse:
        """Run one job immediately and advance its schedule, regardless of
        whether it was actually due. Use this for manual/on-demand firing
        (an ops "run it now" button) as well as from the main loop."""
        now = now or dt.datetime.now(job.timezone)
        self._next[job.name] = job.next_fire(now)

        request = job.build_request()
        log.info("scheduler.fire job=%s agent=%s trace=%s", job.name, job.agent.name, request.trace_id)
        try:
            response = await job.agent.run(request)
        except Exception as exc:
            log.exception("scheduler.job_error job=%s", job.name)
            if self.on_result is not None:
                await _maybe_await(self.on_result(job, exc))
            raise
        if not response.ok:
            log.warning(
                "scheduler.job_not_ok job=%s status=%s error=%s",
                job.name, response.status, response.error,
            )
        if self.on_result is not None:
            await _maybe_await(self.on_result(job, response))
        return response

    async def run_forever(
        self,
        *,
        poll_interval: float = 30.0,
        max_iterations: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Poll for due jobs and run them, forever (or ``max_iterations`` polls
        — set for tests; leave ``None`` in production).

        Jobs run sequentially, deliberately: a scheduler is not the place to
        discover that two BOM sweeps racing each other double-publish a wiki
        page. A job that needs to run concurrently with others should say so
        by delegating internally (subagents), not by this loop overlapping
        two top-level runs.
        """
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            now = dt.datetime.now(dt.timezone.utc)
            for job in self.due_jobs(now):
                try:
                    await self.run_job(job, now=now)
                except Exception:
                    # already logged in run_job; a bad job must not take the
                    # rest of the schedule down with it.
                    continue
            iterations += 1
            if max_iterations is None or iterations < max_iterations:
                await sleep(poll_interval)


async def _maybe_await(value: Any) -> None:
    if isinstance(value, Awaitable):
        await value
