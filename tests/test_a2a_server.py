"""A2A executor tests, against a stub agent -- no credentials, no network.

Built against a2a-sdk 1.1.x's real installed types. ``RequestContext`` needs a
real ``SendMessageRequest`` to populate ``.message`` / ``.get_user_input()``;
constructing it any other way leaves those empty.

Two 1.x facts these tests are shaped around:

- ``TaskStatusUpdateEvent`` has no ``final`` field. Finality is implied by the
  state, so ``final_event`` looks for a terminal state instead of a flag.
- ``Part`` is one flat protobuf message (``text`` / ``raw`` / ``filename`` /
  ``media_type``) rather than a ``root`` wrapping ``TextPart``/``FilePart``.

The executor streams, so a run emits several ``working`` events before its
terminal one. Tests assert on ``final_event`` / ``texts`` rather than on a
fixed event count, so adding a progress event doesn't break every test.
"""

from __future__ import annotations

import asyncio

from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueueLegacy
from a2a.types import Message, Part, Role, SendMessageRequest, TaskState

from deep_research_agent.protocol import AgentRequest, AgentResponse, Citation, Denied, Principal
from deep_research_agent.serving.a2a import DeepAgentExecutor, build_agent_card

ALICE = Principal(subject="alice", division="supply", roles=frozenset({"wiki.reader"}))


def make_context(text: str, metadata: dict | None = None) -> RequestContext:
    msg = Message(
        message_id="m1",
        role=Role.ROLE_USER,
        parts=[Part(text=text)],
        metadata=metadata or {},
    )
    return RequestContext(
        call_context=ServerCallContext(),
        request=SendMessageRequest(message=msg),
        task_id="task-1",
        context_id="ctx-1",
    )


async def drain(queue: EventQueueLegacy) -> list:
    """Every event the executor enqueued, in order.

    Reads the backing ``asyncio.Queue`` directly. 1.x's ``dequeue_event()``
    takes no ``no_wait`` argument and blocks forever once the queue is empty,
    and ``close()`` first would hang on ``join()`` since nothing here marks
    tasks done. Draining the buffer is the only non-blocking option.

    Only ``QueueEmpty`` stops the loop — an earlier version caught every
    exception here, which turned an API mismatch into a silent empty list
    instead of a failure that pointed at the cause.
    """
    events = []
    while True:
        try:
            events.append(queue.queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


def texts(events) -> str:
    """All text across all events -- what a caller would have seen."""
    out = []
    for e in events:
        message = getattr(getattr(e, "status", None), "message", None)
        if message:
            out.extend(p.text for p in message.parts if p.text)
    return "\n".join(out)


TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}


def final_event(events):
    """1.x has no `final` flag -- a terminal state is what ends a task."""
    return next(
        e
        for e in events
        if getattr(getattr(e, "status", None), "state", None) in TERMINAL_STATES
    )


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
    from deep_research_agent.serving.a2a import _default_principal_resolver

    agent = agent or StubAgent()
    resolver = _default_principal_resolver(authorizer, default_principal or ALICE)
    return DeepAgentExecutor(agent=agent, resolve_principal=resolver)


async def run(ex, ctx) -> list:
    queue = EventQueueLegacy()
    await ex.execute(ctx, queue)
    return await drain(queue)


class TestHappyPath:
    async def test_task_text_reaches_the_agent(self):
        agent = StubAgent()
        await run(executor(agent), make_context("what is our exposure on the ASC-4400?"))
        assert agent.calls[0].task == "what is our exposure on the ASC-4400?"

    async def test_the_run_ends_completed(self):
        events = await run(executor(), make_context("hi"))
        assert final_event(events).status.state == TaskState.TASK_STATE_COMPLETED

    async def test_progress_is_streamed_before_the_final_event(self):
        """The point of streaming: a caller sees motion during a long run."""
        events = await run(executor(), make_context("hi"))
        working = [
            e
            for e in events
            if getattr(getattr(e, "status", None), "state", None)
            == TaskState.TASK_STATE_WORKING
        ]
        assert working, "expected at least one working progress event"
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
        assert final_event(events).status.state == TaskState.TASK_STATE_FAILED


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
        assert final_event(events).status.state == TaskState.TASK_STATE_FAILED
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
        assert final_event(events).status.state == TaskState.TASK_STATE_FAILED
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
        assert final_event(events).status.state == TaskState.TASK_STATE_FAILED
        assert "model call failed" in texts(events)

    async def test_agent_response_not_ok_fails_the_a2a_task(self):
        agent = StubAgent(response=AgentResponse(status="refused", output="no access"))
        events = await run(executor(agent), make_context("hi"))
        assert final_event(events).status.state == TaskState.TASK_STATE_FAILED
        assert "no access" in texts(events)

    async def test_partial_status_still_completes_the_a2a_task(self):
        """AgentResponse.ok is True for "partial" -- an A2A caller should see
        completed with the partial content, not a failure."""
        agent = StubAgent(response=AgentResponse(status="partial", output="ran out of turns"))
        events = await run(executor(agent), make_context("hi"))
        assert final_event(events).status.state == TaskState.TASK_STATE_COMPLETED


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
        part = artifact.parts[0]
        assert part.media_type == "text/markdown"
        assert part.filename == "report.md"
        # 1.x carries raw bytes, not a base64 string in a wrapper object.
        assert part.raw == b"# Weekly report\n"

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
        assert final_event(events).status.state == TaskState.TASK_STATE_COMPLETED
        assert not [e for e in events if hasattr(e, "artifact")]


class TestCancel:
    async def test_cancel_publishes_a_canceled_status(self):
        queue = EventQueueLegacy()
        await executor().cancel(make_context("hi"), queue)
        events = await drain(queue)
        assert final_event(events).status.state == TaskState.TASK_STATE_CANCELED


class TestAgentCard:
    def test_card_exposes_a_skill_per_registered_agent(self):
        from deep_research_agent.mesh import AgentRegistry
        from deep_research_agent.protocol import AgentSpec

        async def handler(request):
            return AgentResponse(status="ok")

        registry = AgentRegistry()
        registry.register(AgentSpec(name="deep_research_agent", description="d1", handler=handler))
        registry.register(AgentSpec(name="wiki_ask", description="d2", handler=handler))

        card = build_agent_card(
            StubAgent(name="deep_research_agent"), url="http://localhost:8000/", registry=registry
        )
        assert {s.id for s in card.skills} == {"deep_research_agent", "wiki_ask"}

    def test_card_falls_back_to_the_bare_agent_with_no_registry(self):
        agent = StubAgent(name="deep_research_agent", description="deep research agent")
        card = build_agent_card(agent, url="http://localhost:8000/")
        assert [s.id for s in card.skills] == ["deep_research_agent"]
        assert card.name == "deep_research_agent"

    def test_card_advertises_the_configured_url(self):
        """1.x replaced AgentCard.url with a supported_interfaces list."""
        card = build_agent_card(StubAgent(), url="http://example.internal:9000/")
        assert [i.url for i in card.supported_interfaces] == ["http://example.internal:9000/"]
        assert card.supported_interfaces[0].protocol_version == "1.0"

    def test_card_advertises_streaming(self):
        card = build_agent_card(StubAgent(), url="http://x/")
        assert card.capabilities.streaming is True


class TestThroughTheRealHandler:
    """One round trip through the wired app: HTTP -> JSON-RPC -> handler ->
    executor -> stub agent.

    The unit tests above hand the executor a bare queue, which accepts any
    event order. The real ``DefaultRequestHandler`` does not: it rejects a
    status update that arrives before a ``Task`` has been opened. That gap let
    a genuine bug through the whole unit suite, so this exercises the seam the
    others cannot reach.
    """

    def app(self, agent=None):
        from fastapi.testclient import TestClient

        from deep_research_agent.serving.a2a import build_a2a_app

        return TestClient(
            build_a2a_app(agent or StubAgent(), url="http://test/", default_principal=ALICE)
        )

    def send(self, client, body, headers=None):
        return client.post("/", json=body, headers=headers or {}).json()

    def v1_body(self, text="hi", metadata=None):
        message = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}]}
        if metadata:
            message["metadata"] = metadata
        return {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": {"message": message},
        }

    def test_a_v1_call_completes_end_to_end(self):
        agent = StubAgent(response=AgentResponse(status="ok", output="Acme is sole source."))
        result = self.send(
            self.app(agent), self.v1_body("who supplies it?"), {"A2A-Version": "1.0"}
        )["result"]
        task = result.get("task", result)
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert "Acme is sole source." in task["status"]["message"]["parts"][0]["text"]

    def test_the_answer_rides_on_the_terminal_event(self):
        """A non-streaming caller only ever sees the final Task. If the answer
        were relayed on a preceding `working` update it would arrive empty."""
        agent = StubAgent(response=AgentResponse(status="ok", output="the answer"))
        result = self.send(self.app(agent), self.v1_body(), {"A2A-Version": "1.0"})["result"]
        task = result.get("task", result)
        assert "the answer" in task["status"]["message"]["parts"][0]["text"]

    def test_structured_inputs_survive_the_protobuf_struct_round_trip(self):
        """Nested metadata is a protobuf Struct on the wire; a naive dict()
        conversion leaves `inputs` unconverted and it gets dropped."""
        agent = StubAgent()
        self.send(
            self.app(agent),
            self.v1_body(metadata={"inputs": {"tier": "critical"}}),
            {"A2A-Version": "1.0"},
        )
        assert agent.calls[0].inputs == {"tier": "critical"}

    def test_a_v1_body_without_the_version_header_is_a_version_mismatch(self):
        """A missing header means 0.3, not "unversioned" -- so a 1.0 body sent
        without it is rejected as a mismatch rather than served."""
        error = self.send(self.app(), self.v1_body())["error"]
        assert "0.3" in str(error["message"])

    def test_a_v03_caller_still_works_through_the_compat_adapter(self):
        agent = StubAgent(response=AgentResponse(status="ok", output="legacy ok"))
        body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "m1",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hi"}],
                }
            },
        }
        result = self.send(self.app(agent), body)["result"]
        task = result.get("task", result)
        assert task["status"]["state"] == "completed"
        assert "legacy ok" in task["status"]["message"]["parts"][0]["text"]

    def test_the_agent_card_advertises_the_configured_url(self):
        card = self.app().get("/.well-known/agent-card.json").json()
        assert [i["url"] for i in card["supportedInterfaces"]] == ["http://test/"]
