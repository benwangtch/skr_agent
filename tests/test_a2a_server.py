"""A2A executor tests, against a stub agent -- no credentials, no network.

Built against a2a-sdk 1.1.x's real installed types (confirmed by
introspection while writing serving/a2a.py, not copied from tutorials that
target an older, since-removed convenience API). ``RequestContext`` needs a
real ``SendMessageRequest`` to populate ``.message`` / ``.get_user_input()`` —
constructing it any other way leaves those empty.
"""

from __future__ import annotations

from a2a.server.agent_execution.context import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import Message, Part, Role, SendMessageRequest, TaskState

from skr_agent.protocol import AgentRequest, AgentResponse, Citation, Denied, Principal
from skr_agent.serving.a2a import DeepAgentExecutor, build_agent_card

ALICE = Principal(subject="alice", division="supply", roles=frozenset({"wiki.reader"}))


def make_context(text: str, metadata: dict | None = None) -> RequestContext:
    msg = Message(
        message_id="m1", role=Role.ROLE_USER, parts=[Part(text=text)], metadata=metadata or {}
    )
    return RequestContext(call_context=ServerCallContext(), request=SendMessageRequest(message=msg))


async def drain(queue: EventQueueLegacy, count: int) -> list:
    return [await queue.dequeue_event() for _ in range(count)]


class StubAgent:
    def __init__(self, name="stub", description="a stub agent", response=None, error=None):
        self.name = name
        self.description = description
        self.response = response or AgentResponse(status="ok", output="the answer")
        self.error = error
        self.calls: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.response


def executor(agent=None, *, authorizer=None, default_principal=None):
    from skr_agent.serving.a2a import _default_principal_resolver

    agent = agent or StubAgent()
    default_principal = default_principal or ALICE
    resolver = _default_principal_resolver(authorizer, default_principal)
    return DeepAgentExecutor(agent=agent, resolve_principal=resolver)


class TestHappyPath:
    async def test_task_text_reaches_the_agent(self):
        agent = StubAgent()
        ex = executor(agent)
        ctx = make_context("what is our exposure on the ASC-4400?")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        assert agent.calls[0].task == "what is our exposure on the ASC-4400?"

    async def test_events_are_submitted_working_completed_in_order(self):
        ex = executor()
        ctx = make_context("hi")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        events = await drain(queue, 3)
        states = [e.status.state for e in events]
        assert states == [
            TaskState.TASK_STATE_SUBMITTED,
            TaskState.TASK_STATE_WORKING,
            TaskState.TASK_STATE_COMPLETED,
        ]

    async def test_final_message_carries_the_agent_output(self):
        agent = StubAgent(response=AgentResponse(status="ok", output="Acme is sole source."))
        ex = executor(agent)
        ctx = make_context("who supplies the ASC-4400?")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        events = await drain(queue, 3)
        text = events[-1].status.message.parts[0].text
        assert "Acme is sole source." in text

    async def test_citations_are_appended_to_the_final_message(self):
        response = AgentResponse(
            status="ok",
            output="Acme is sole source.",
            citations=(Citation(kind="wiki_page", ref="supply/acme", title="Acme profile"),),
        )
        ex = executor(StubAgent(response=response))
        ctx = make_context("who supplies the ASC-4400?")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        events = await drain(queue, 3)
        parts_text = "\n".join(p.text for p in events[-1].status.message.parts)
        assert "supply/acme" in parts_text

    async def test_task_id_is_threaded_through_as_the_trace_id(self):
        agent = StubAgent()
        ex = executor(agent)
        ctx = make_context("hi")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        assert agent.calls[0].trace_id == ctx.task_id


class TestStructuredInputs:
    async def test_inputs_metadata_reaches_the_agent_request(self):
        agent = StubAgent()
        ex = executor(agent)
        ctx = make_context("sweep it", metadata={"inputs": {"tier": "critical"}})
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        assert agent.calls[0].inputs == {"tier": "critical"}

    async def test_missing_inputs_defaults_to_empty_dict(self):
        agent = StubAgent()
        ex = executor(agent)
        ctx = make_context("hi")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        assert agent.calls[0].inputs == {}

    async def test_nested_inputs_survive_the_protobuf_struct_round_trip(self):
        """The bug this guards: naive dict(message.metadata) leaves nested
        values as unconverted protobuf objects, which then fail an
        isinstance(dict) check and get silently dropped."""
        agent = StubAgent()
        ex = executor(agent)
        ctx = make_context(
            "sweep it", metadata={"inputs": {"tier": "critical", "companies": ["a", "b"]}}
        )
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        assert agent.calls[0].inputs == {"tier": "critical", "companies": ["a", "b"]}


class TestPrincipalResolution:
    async def test_no_authorizer_uses_the_default_principal(self):
        agent = StubAgent()
        ex = executor(agent, default_principal=ALICE)
        ctx = make_context("hi")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        assert agent.calls[0].principal is ALICE

    async def test_authorizer_verifies_the_token_in_metadata(self):
        class FakeAuthorizer:
            def verify(self, token: str) -> Principal:
                assert token == "trusted-token"
                return Principal(subject="verified-user", division="finance")

        agent = StubAgent()
        ex = executor(agent, authorizer=FakeAuthorizer())
        ctx = make_context("hi", metadata={"token": "trusted-token"})
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        assert agent.calls[0].principal.subject == "verified-user"

    async def test_missing_token_with_an_authorizer_configured_is_refused(self):
        class FakeAuthorizer:
            def verify(self, token: str) -> Principal:
                raise AssertionError("should not be called with no token")

        agent = StubAgent()
        ex = executor(agent, authorizer=FakeAuthorizer())
        ctx = make_context("hi")  # no token in metadata
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        assert agent.calls == []
        events = await drain(queue, 3)
        assert events[-1].status.state == TaskState.TASK_STATE_FAILED
        assert "Permission denied" in events[-1].status.message.parts[0].text

    async def test_authorizer_rejecting_the_token_fails_the_task_not_the_process(self):
        class RejectingAuthorizer:
            def verify(self, token: str) -> Principal:
                raise Denied("token expired")

        agent = StubAgent()
        ex = executor(agent, authorizer=RejectingAuthorizer())
        ctx = make_context("hi", metadata={"token": "expired-token"})
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)  # must not raise
        assert agent.calls == []
        events = await drain(queue, 3)
        assert events[-1].status.state == TaskState.TASK_STATE_FAILED
        assert "token expired" in events[-1].status.message.parts[0].text

    async def test_authorizer_never_falls_back_to_the_default_principal(self):
        """A configured authorizer that rejects must not silently downgrade
        to default_principal -- that would make the authorizer decorative."""

        class AlwaysRejects:
            def verify(self, token: str) -> Principal:
                raise Denied("nope")

        agent = StubAgent()
        ex = executor(agent, authorizer=AlwaysRejects(), default_principal=ALICE)
        ctx = make_context("hi", metadata={"token": "anything"})
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        assert agent.calls == []


class TestFailurePaths:
    async def test_agent_error_fails_the_task_with_the_error_text(self):
        agent = StubAgent(error=RuntimeError("model call failed"))
        ex = executor(agent)
        ctx = make_context("hi")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)  # must not raise
        events = await drain(queue, 3)
        assert events[-1].status.state == TaskState.TASK_STATE_FAILED
        assert "model call failed" in events[-1].status.message.parts[0].text

    async def test_agent_response_not_ok_fails_the_a2a_task(self):
        agent = StubAgent(response=AgentResponse(status="refused", output="no access"))
        ex = executor(agent)
        ctx = make_context("hi")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        events = await drain(queue, 3)
        assert events[-1].status.state == TaskState.TASK_STATE_FAILED

    async def test_partial_status_still_completes_the_a2a_task(self):
        """AgentResponse.ok is True for "partial" -- an A2A caller should see
        completed with the partial content, not a failure."""
        agent = StubAgent(response=AgentResponse(status="partial", output="ran out of turns"))
        ex = executor(agent)
        ctx = make_context("hi")
        queue = EventQueueLegacy()
        await ex.execute(ctx, queue)
        events = await drain(queue, 3)
        assert events[-1].status.state == TaskState.TASK_STATE_COMPLETED


class TestCancel:
    async def test_cancel_publishes_a_canceled_status(self):
        ex = executor()
        ctx = make_context("hi")
        queue = EventQueueLegacy()
        await ex.cancel(ctx, queue)
        event = await queue.dequeue_event()
        assert event.status.state == TaskState.TASK_STATE_CANCELED


class TestAgentCard:
    def test_card_exposes_a_skill_per_registered_agent(self):
        from skr_agent.mesh import AgentRegistry
        from skr_agent.protocol import AgentSpec

        async def handler(request):
            return AgentResponse(status="ok")

        registry = AgentRegistry()
        registry.register(AgentSpec(name="wiki_report", description="d1", handler=handler))
        registry.register(AgentSpec(name="wiki_ask", description="d2", handler=handler))

        agent = StubAgent(name="wiki_report")
        card = build_agent_card(agent, base_url="http://localhost:8000/", registry=registry)
        assert {s.id for s in card.skills} == {"wiki_report", "wiki_ask"}

    def test_card_falls_back_to_the_bare_agent_with_no_registry(self):
        agent = StubAgent(name="wiki_report", description="deep research agent")
        card = build_agent_card(agent, base_url="http://localhost:8000/")
        assert [s.id for s in card.skills] == ["wiki_report"]
        assert card.name == "wiki_report"

    def test_card_url_is_the_configured_base_url(self):
        agent = StubAgent()
        card = build_agent_card(agent, base_url="http://example.internal:9000/")
        assert card.supported_interfaces[0].url == "http://example.internal:9000/"
