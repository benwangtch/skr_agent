"""A2A executor tests, against a stub agent -- no credentials, no network.

Built against a2a-sdk 0.3.x's real installed types — the generation the
team's reference server uses. ``RequestContext`` needs a real
``MessageSendParams`` to populate ``.message`` / ``.get_user_input()``;
constructing it any other way leaves those empty.

The executor streams, so a run emits several non-final ``working`` events
before its final one. Tests assert on ``final_event`` / ``texts`` rather than
on a fixed event count, so adding a progress event doesn't break every test.
"""

from __future__ import annotations

from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.types import Message, MessageSendParams, Part, Role, TaskState, TextPart

from skr_agent.protocol import AgentRequest, AgentResponse, Citation, Denied, Principal
from skr_agent.serving.a2a import DeepAgentExecutor, build_agent_card

ALICE = Principal(subject="alice", division="supply", roles=frozenset({"wiki.reader"}))


def make_context(text: str, metadata: dict | None = None) -> RequestContext:
    msg = Message(
        message_id="m1",
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        metadata=metadata or {},
    )
    return RequestContext(
        call_context=ServerCallContext(),
        request=MessageSendParams(message=msg),
        task_id="task-1",
        context_id="ctx-1",
    )


async def drain(queue: EventQueue) -> list:
    """Every event the executor enqueued, in order.

    Deliberately does not ``close()`` the queue first: a non-immediate close
    awaits ``queue.join()``, nothing here marks tasks done, and the test hangs.
    """
    events = []
    while True:
        try:
            events.append(await queue.dequeue_event(no_wait=True))
        except Exception:
            return events


def texts(events) -> str:
    """All text across all events -- what a caller would have seen."""
    out = []
    for e in events:
        message = getattr(getattr(e, "status", None), "message", None)
        if message:
            out.extend(p.root.text for p in message.parts if hasattr(p.root, "text"))
    return "\n".join(out)


def final_event(events):
    return next(e for e in events if getattr(e, "final", False))


class StubAgent:
    """Stands in for a DeepAgent. Streams like one: progress, then a response."""

    def __init__(self, name="stub", description="a stub agent", response=None, error=None):
        self.name = name
        self.description = description
        self.response = response or AgentResponse(status="ok", output="the answer")
        self.error = error
        self.calls: list[AgentRequest] = []

    async def stream(self, request: AgentRequest):
        self.calls.append(request)
        if self.error:
            raise self.error
        yield ("progress", "Working: some_tool")
        yield self.response


def executor(agent=None, *, authorizer=None, default_principal=None):
    from skr_agent.serving.a2a import _default_principal_resolver

    agent = agent or StubAgent()
    resolver = _default_principal_resolver(authorizer, default_principal or ALICE)
    return DeepAgentExecutor(agent=agent, resolve_principal=resolver)


async def run(ex, ctx) -> list:
    queue = EventQueue()
    await ex.execute(ctx, queue)
    return await drain(queue)


class TestHappyPath:
    async def test_task_text_reaches_the_agent(self):
        agent = StubAgent()
        await run(executor(agent), make_context("what is our exposure on the ASC-4400?"))
        assert agent.calls[0].task == "what is our exposure on the ASC-4400?"

    async def test_the_run_ends_completed(self):
        events = await run(executor(), make_context("hi"))
        assert final_event(events).status.state == TaskState.completed

    async def test_progress_is_streamed_before_the_final_event(self):
        """The point of streaming: a caller sees motion during a long run."""
        events = await run(executor(), make_context("hi"))
        working = [e for e in events if not e.final]
        assert working, "expected at least one non-final progress event"
        assert all(e.status.state == TaskState.working for e in working)
        assert "Working: some_tool" in texts(events)

    async def test_agent_output_reaches_the_caller(self):
        agent = StubAgent(response=AgentResponse(status="ok", output="Acme is sole source."))
        events = await run(executor(agent), make_context("who supplies the ASC-4400?"))
        assert "Acme is sole source." in texts(events)

    async def test_citations_are_relayed(self):
        response = AgentResponse(
            status="ok",
            output="Acme is sole source.",
            citations=(Citation(kind="wiki_page", ref="supply/acme", title="Acme profile"),),
        )
        events = await run(executor(StubAgent(response=response)), make_context("q"))
        assert "supply/acme" in texts(events)

    async def test_task_id_is_threaded_through_as_the_trace_id(self):
        agent = StubAgent()
        ctx = make_context("hi")
        await run(executor(agent), ctx)
        assert agent.calls[0].trace_id == ctx.task_id

    async def test_empty_input_fails_without_calling_the_agent(self):
        agent = StubAgent()
        events = await run(executor(agent), make_context(""))
        assert agent.calls == []
        assert final_event(events).status.state == TaskState.failed


class TestStructuredInputs:
    async def test_inputs_metadata_reaches_the_agent_request(self):
        agent = StubAgent()
        await run(executor(agent), make_context("sweep", metadata={"inputs": {"tier": "critical"}}))
        assert agent.calls[0].inputs == {"tier": "critical"}

    async def test_missing_inputs_defaults_to_empty_dict(self):
        agent = StubAgent()
        await run(executor(agent), make_context("hi"))
        assert agent.calls[0].inputs == {}

    async def test_nested_inputs_survive_intact(self):
        agent = StubAgent()
        await run(
            executor(agent),
            make_context("sweep", metadata={"inputs": {"tier": "critical", "companies": ["a", "b"]}}),
        )
        assert agent.calls[0].inputs == {"tier": "critical", "companies": ["a", "b"]}

    async def test_non_dict_inputs_are_ignored_rather_than_crashing(self):
        agent = StubAgent()
        await run(executor(agent), make_context("hi", metadata={"inputs": "not-a-dict"}))
        assert agent.calls[0].inputs == {}


class TestPrincipalResolution:
    async def test_no_authorizer_uses_the_default_principal(self):
        agent = StubAgent()
        await run(executor(agent, default_principal=ALICE), make_context("hi"))
        assert agent.calls[0].principal is ALICE

    async def test_authorizer_verifies_the_token_in_metadata(self):
        class FakeAuthorizer:
            def verify(self, token: str) -> Principal:
                assert token == "trusted-token"
                return Principal(subject="verified-user", division="finance")

        agent = StubAgent()
        await run(
            executor(agent, authorizer=FakeAuthorizer()),
            make_context("hi", metadata={"token": "trusted-token"}),
        )
        assert agent.calls[0].principal.subject == "verified-user"

    async def test_missing_token_with_an_authorizer_configured_is_refused(self):
        class FakeAuthorizer:
            def verify(self, token: str) -> Principal:
                raise AssertionError("should not be called with no token")

        agent = StubAgent()
        events = await run(executor(agent, authorizer=FakeAuthorizer()), make_context("hi"))
        assert agent.calls == []
        assert final_event(events).status.state == TaskState.failed
        assert "Permission denied" in texts(events)

    async def test_authorizer_rejecting_the_token_fails_the_task_not_the_process(self):
        class RejectingAuthorizer:
            def verify(self, token: str) -> Principal:
                raise Denied("token expired")

        agent = StubAgent()
        events = await run(
            executor(agent, authorizer=RejectingAuthorizer()),
            make_context("hi", metadata={"token": "expired-token"}),
        )
        assert agent.calls == []
        assert final_event(events).status.state == TaskState.failed
        assert "token expired" in texts(events)

    async def test_authorizer_never_falls_back_to_the_default_principal(self):
        """A configured authorizer that rejects must not silently downgrade
        to default_principal -- that would make the authorizer decorative."""

        class AlwaysRejects:
            def verify(self, token: str) -> Principal:
                raise Denied("nope")

        agent = StubAgent()
        await run(
            executor(agent, authorizer=AlwaysRejects(), default_principal=ALICE),
            make_context("hi", metadata={"token": "anything"}),
        )
        assert agent.calls == []


class TestFailurePaths:
    async def test_agent_error_fails_the_task_with_the_error_text(self):
        agent = StubAgent(error=RuntimeError("model call failed"))
        events = await run(executor(agent), make_context("hi"))  # must not raise
        assert final_event(events).status.state == TaskState.failed
        assert "model call failed" in texts(events)

    async def test_agent_response_not_ok_fails_the_a2a_task(self):
        agent = StubAgent(response=AgentResponse(status="refused", output="no access"))
        events = await run(executor(agent), make_context("hi"))
        assert final_event(events).status.state == TaskState.failed
        assert "no access" in texts(events)

    async def test_partial_status_still_completes_the_a2a_task(self):
        """AgentResponse.ok is True for "partial" -- an A2A caller should see
        completed with the partial content, not a failure."""
        agent = StubAgent(response=AgentResponse(status="partial", output="ran out of turns"))
        events = await run(executor(agent), make_context("hi"))
        assert final_event(events).status.state == TaskState.completed


class TestFileArtifacts:
    """The platform's `<render-cpochat .../>` convention for surfacing files."""

    async def test_render_tag_becomes_a_file_artifact(self, tmp_path):
        report = tmp_path / "report.md"
        report.write_text("# Weekly report\n")
        agent = StubAgent(
            response=AgentResponse(
                status="ok",
                output=f'Done.\n<render-cpochat src="{report}" name="report.md" desc="Weekly" />\nBye.',
            )
        )
        events = await run(executor(agent), make_context("hi"))

        artifacts = [e for e in events if hasattr(e, "artifact")]
        assert len(artifacts) == 1
        artifact = artifacts[0].artifact
        assert artifact.name == "report.md"
        assert artifact.description == "Weekly"
        assert artifact.parts[0].root.file.mime_type == "text/markdown"

        # The surrounding prose still reaches the caller as text.
        body = texts(events)
        assert "Done." in body and "Bye." in body

    async def test_a_missing_file_is_skipped_rather_than_failing_the_task(self):
        agent = StubAgent(
            response=AgentResponse(
                status="ok", output='<render-cpochat src="/no/such/file.md" name="x.md" />ok'
            )
        )
        events = await run(executor(agent), make_context("hi"))
        assert final_event(events).status.state == TaskState.completed
        assert not [e for e in events if hasattr(e, "artifact")]


class TestCancel:
    async def test_cancel_publishes_a_canceled_status(self):
        queue = EventQueue()
        await executor().cancel(make_context("hi"), queue)
        events = await drain(queue)
        assert final_event(events).status.state == TaskState.canceled


class TestAgentCard:
    def test_card_exposes_a_skill_per_registered_agent(self):
        from skr_agent.mesh import AgentRegistry
        from skr_agent.protocol import AgentSpec

        async def handler(request):
            return AgentResponse(status="ok")

        registry = AgentRegistry()
        registry.register(AgentSpec(name="skr_agent", description="d1", handler=handler))
        registry.register(AgentSpec(name="wiki_ask", description="d2", handler=handler))

        card = build_agent_card(
            StubAgent(name="skr_agent"), url="http://localhost:8000/", registry=registry
        )
        assert {s.id for s in card.skills} == {"skr_agent", "wiki_ask"}

    def test_card_falls_back_to_the_bare_agent_with_no_registry(self):
        agent = StubAgent(name="skr_agent", description="deep research agent")
        card = build_agent_card(agent, url="http://localhost:8000/")
        assert [s.id for s in card.skills] == ["skr_agent"]
        assert card.name == "skr_agent"

    def test_card_url_is_the_configured_url(self):
        card = build_agent_card(StubAgent(), url="http://example.internal:9000/")
        assert card.url == "http://example.internal:9000/"

    def test_card_advertises_streaming(self):
        card = build_agent_card(StubAgent(), url="http://x/")
        assert card.capabilities.streaming is True
