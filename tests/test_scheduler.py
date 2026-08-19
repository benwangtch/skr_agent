"""Scheduler tests. No credentials needed — jobs run against a stub agent
that never touches the agent framework, since the scheduler's job is timing
and lifecycle, not model behavior (that's DeepAgent's job, tested elsewhere)."""

from __future__ import annotations

import datetime as dt

import pytest

from deep_research_agent.protocol import AgentRequest, AgentResponse, Principal
from deep_research_agent.serving.scheduler import ScheduledJob, Scheduler

ALICE = Principal(subject="alice", division="supply")
UTC = dt.timezone.utc


class StubAgent:
    """Satisfies the one method Scheduler needs from an agent: .run()."""

    def __init__(self, name="stub", response=None, error=None):
        self.name = name
        self.response = response or AgentResponse(status="ok", output="done")
        self.error = error
        self.calls: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.response


def job(cron="0 8 * * 1", **overrides) -> ScheduledJob:
    defaults = dict(
        name="test-job",
        cron=cron,
        agent=StubAgent(),
        task="do the thing",
        principal=ALICE,
    )
    defaults.update(overrides)
    return ScheduledJob(**defaults)


class TestValidation:
    def test_invalid_cron_raises_at_construction(self):
        with pytest.raises(ValueError, match="invalid cron"):
            job(cron="not a cron expression")

    def test_valid_cron_constructs_fine(self):
        job(cron="*/5 * * * *")  # no raise


class TestNextFire:
    def test_next_fire_is_strictly_after_the_given_time(self):
        j = job(cron="0 8 * * 1")  # Monday 08:00
        now = dt.datetime(2026, 8, 4, 12, 0, tzinfo=UTC)  # Tuesday
        nxt = j.next_fire(now)
        assert nxt > now
        assert nxt.weekday() == 0  # Monday
        assert nxt.hour == 8

    def test_every_minute_cron_fires_within_a_minute(self):
        j = job(cron="* * * * *")
        now = dt.datetime(2026, 8, 4, 12, 0, 30, tzinfo=UTC)
        nxt = j.next_fire(now)
        assert nxt - now <= dt.timedelta(minutes=1)


class TestBuildRequest:
    def test_fixed_task_and_principal(self):
        j = job(task="fixed task", principal=ALICE, inputs={"k": "v"})
        req = j.build_request()
        assert req.task == "fixed task"
        assert req.principal is ALICE
        assert req.inputs == {"k": "v"}

    def test_callable_task_and_principal_are_resolved_fresh(self):
        calls = {"principal": 0, "task": 0}

        def make_principal():
            calls["principal"] += 1
            return Principal(subject=f"svc-{calls['principal']}", division="exec")

        def make_task():
            calls["task"] += 1
            return f"task #{calls['task']}"

        j = job(task=make_task, principal=make_principal)
        req1 = j.build_request()
        req2 = j.build_request()
        assert req1.principal.subject == "svc-1"
        assert req2.principal.subject == "svc-2"
        assert req1.task == "task #1"
        assert req2.task == "task #2"

    def test_missing_inputs_defaults_to_empty_dict(self):
        j = job()
        assert j.build_request().inputs == {}


class TestDueJobs:
    def test_job_not_due_before_its_first_scheduled_fire(self):
        s = Scheduler([job(cron="0 8 * * 1")])
        now = dt.datetime(2026, 8, 4, 7, 0, tzinfo=UTC)  # before Monday 08:00
        assert s.due_jobs(now) == []

    def test_job_due_once_scheduled_time_arrives(self):
        s = Scheduler([job(name="every-minute", cron="* * * * *")])
        now = dt.datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        s._seed(now)
        due_time = s._next["every-minute"]
        assert s.due_jobs(due_time) != []

    def test_due_jobs_does_not_advance_schedule(self):
        """Calling due_jobs() repeatedly must be side-effect free -- only
        run_job() advances a job's next-fire time."""
        s = Scheduler([job(name="j", cron="* * * * *")])
        now = dt.datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        first = s.due_jobs(now)
        second = s.due_jobs(now)
        assert first == second


class TestRunJob:
    async def test_run_job_invokes_the_agent_with_a_built_request(self):
        agent = StubAgent()
        s = Scheduler([])
        j = job(agent=agent, task="sweep it")
        response = await s.run_job(j)
        assert response.status == "ok"
        assert len(agent.calls) == 1
        assert agent.calls[0].task == "sweep it"

    async def test_run_job_advances_the_schedule(self):
        s = Scheduler([])
        j = job(cron="0 8 * * 1")
        now = dt.datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        await s.run_job(j, now=now)
        assert s._next[j.name] > now

    async def test_run_job_propagates_and_logs_agent_exceptions(self):
        agent = StubAgent(error=RuntimeError("model call failed"))
        s = Scheduler([])
        j = job(agent=agent)
        with pytest.raises(RuntimeError, match="model call failed"):
            await s.run_job(j)

    async def test_on_result_hook_fires_on_success(self):
        seen = []
        s = Scheduler([], on_result=lambda job, result: seen.append((job.name, result)))
        j = job(name="hooked")
        await s.run_job(j)
        assert seen[0][0] == "hooked"
        assert isinstance(seen[0][1], AgentResponse)

    async def test_on_result_hook_fires_on_failure_with_the_exception(self):
        seen = []
        s = Scheduler([], on_result=lambda job, result: seen.append(result))
        j = job(agent=StubAgent(error=ValueError("boom")), name="hooked")
        with pytest.raises(ValueError):
            await s.run_job(j)
        assert isinstance(seen[0], ValueError)

    async def test_async_on_result_hook_is_awaited(self):
        seen = []

        async def hook(job, result):
            seen.append(result)

        s = Scheduler([], on_result=hook)
        await s.run_job(job())
        assert len(seen) == 1


def _seed_due_now(scheduler: Scheduler) -> None:
    """Force every job in the scheduler to be due on the loop's first tick.

    A freshly seeded job's next fire is always strictly in the future (real
    cron semantics: a job doesn't fire just because the process restarted),
    so ``run_forever``'s loop mechanics -- iterate, run due jobs, tolerate
    failures, sleep between ticks -- can't be exercised by waiting for real
    wall-clock time to elapse in a fast test. Reaching into ``_next`` directly
    is legitimate here: it's the same seam ``TestDueJobs`` already uses.
    """
    epoch = dt.datetime(1970, 1, 1, tzinfo=UTC)
    for j in scheduler.jobs:
        scheduler._next[j.name] = epoch


class TestRunForever:
    async def test_bounded_loop_runs_due_jobs_and_returns(self):
        agent = StubAgent()
        s = Scheduler([job(agent=agent, cron="* * * * *")])
        _seed_due_now(s)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        await s.run_forever(poll_interval=5, max_iterations=2, sleep=fake_sleep)
        assert len(agent.calls) >= 1
        assert sleeps == [5]  # slept once between the two iterations, not after the last

    async def test_a_failing_job_does_not_stop_other_jobs_in_the_same_tick(self):
        good = StubAgent()
        bad = StubAgent(error=RuntimeError("boom"))
        s = Scheduler(
            [
                job(name="bad", agent=bad, cron="* * * * *"),
                job(name="good", agent=good, cron="* * * * *"),
            ]
        )
        _seed_due_now(s)

        async def fake_sleep(seconds: float) -> None:
            return None

        await s.run_forever(poll_interval=1, max_iterations=1, sleep=fake_sleep)
        assert len(good.calls) == 1

    async def test_max_iterations_zero_runs_nothing(self):
        agent = StubAgent()
        s = Scheduler([job(agent=agent, cron="* * * * *")])

        async def fake_sleep(seconds: float) -> None:
            raise AssertionError("should not sleep")

        await s.run_forever(max_iterations=0, sleep=fake_sleep)
        assert agent.calls == []
