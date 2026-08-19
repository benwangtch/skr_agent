"""Serve a ``DeepAgent`` as an A2A (Agent2Agent) server.

Written against ``a2a-sdk`` 1.1.x, whose API differs from the 0.3.x line this
module previously targeted in almost every particular. The differences that
actually forced code changes, all confirmed by importing and inspecting the
installed package rather than copied from a tutorial:

- ``a2a.server.apps`` is gone. There is no ``A2AStarletteApplication``; you
  build routes (``create_agent_card_routes`` / ``create_jsonrpc_routes`` /
  ``create_rest_routes``) and attach them to your own app with
  ``add_a2a_routes_to_fastapi``. Hence this returns a ``FastAPI``, not a
  wrapper object with a ``.build()``.
- **The types are protobuf now**, not pydantic. ``TextPart`` / ``FilePart`` /
  ``FileWithBytes`` no longer exist: ``Part`` is one flat message with a
  ``content`` oneof (``text`` / ``raw`` / ``url`` / ``data``) plus ``filename``
  and ``media_type``. A file part carries raw ``bytes`` — no base64 wrapper.
- ``TaskStatusUpdateEvent`` has **no ``final`` field**. Terminality is implied
  by the state, and ``TaskUpdater`` enforces it: any status update after a
  terminal one raises.
- Enum members are prefixed (``TaskState.TASK_STATE_COMPLETED``), and
  ``Role.ROLE_AGENT`` likewise.
- ``AgentCard.url`` is gone, replaced by ``supported_interfaces`` — a card now
  advertises one entry per transport rather than a single URL.
- **A missing ``A2A-Version`` header means 0.3, it is not an error.** The SDK
  defaults the absent header to ``"0.3"`` and routes accordingly, so what a
  caller actually has to keep consistent is the header *and* the body shape
  together. Measured against this app, with ``enable_v0_3_compat=True``:

  ==========================  ====================  ==========================
  Header                      Body                  Result
  ==========================  ====================  ==========================
  ``A2A-Version: 1.0``        1.0 (``SendMessage``  works
                              , ``ROLE_USER``)
  *absent*                    0.3 (``message/send`` works, via the compat
                              , ``user``)           adapter
  *absent*                    1.0                   ``VERSION_NOT_SUPPORTED``
  ==========================  ====================  ==========================

  So the header is effectively required to reach the 1.0 path, but the failure
  mode is a version *mismatch* against the 0.3 default rather than a rejection
  for a missing header.

Two things this module adds over a minimal executor:

- **Progress streaming.** A BOM sweep takes minutes. Every step the agent
  takes is relayed as a ``working`` status update so a caller sees motion
  instead of a silent connection.
- **Real caller authentication, as a seam.** ``PrincipalResolver`` is where a
  real ``Authorizer`` (see ``protocol.py``) verifies a bearer token. Left
  unconfigured, every caller gets ``default_principal``, which is deliberately
  read-only and scoped to the shared namespace — loud enough to notice, not so
  open that a forgotten config accidentally exposes a write path.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import (
    PROTOCOL_VERSION_1_0,
    add_a2a_routes_to_fastapi,
)
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
)
from a2a.utils import TransportProtocol
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict

from deep_research_agent.mesh import AgentRegistry
from deep_research_agent.protocol import AgentRequest, AgentResponse, Authorizer, Denied, Principal
from deep_research_agent.runtime import DeepAgent

log = logging.getLogger(__name__)

__all__ = ["build_agent_card", "DeepAgentExecutor", "build_a2a_app", "serve"]

PrincipalResolver = Callable[[dict[str, Any], Any], Principal]

# The platform's convention for surfacing a file in chat. An agent emits
# `<render-cpochat src="..." name="..." desc="..." />` inline in its answer and
# the server turns it into an A2A file artifact.
RENDER_TAG = re.compile(r"(<render-cpochat[^>]*/>)")
_TAG_ATTR = re.compile(r"(\w+)=['\"]([^'\"]*)['\"]")

_MIME_TYPES = {
    ".py": "text/x-python",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
}


def _mime_type(filename: str) -> str:
    return _MIME_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def _message_metadata(message: Message | None) -> dict[str, Any]:
    """``message.metadata`` is a protobuf ``Struct``, not a Python dict.

    Naive ``dict(message.metadata)`` converts only the top level — a nested
    object (like ``inputs``) comes back as an unconverted ``Struct``, which
    then fails the ``isinstance(x, dict)`` check below and is silently
    dropped. ``MessageToDict`` recurses properly.

    One thing it cannot avoid: protobuf's JSON ``Value`` has no integer type,
    so a caller sending ``{"inputs": {"count": 3}}`` gets ``3.0`` back. Accept
    floats on the receiving end for anything that arrives this way.
    """
    if message is None:
        return {}
    return MessageToDict(message.metadata)


def _default_principal_resolver(
    authorizer: Authorizer | None, default_principal: Principal
) -> PrincipalResolver:
    """The seam for real auth. See module docstring.

    With no ``authorizer``, every call runs as ``default_principal`` and this
    is logged once at server startup (see ``build_a2a_app``), not per-request —
    per-request would either spam logs or get silenced, and a silenced warning
    defeats the point.

    With an ``authorizer``, the incoming message's ``metadata["token"]`` is
    verified the same way any credential is verified at a trust boundary in
    this codebase: re-derive authority, don't trust caller-supplied claims. A
    message with no token, or a token that fails verification, is refused — it
    does not fall back to ``default_principal``, which would make the
    authorizer decorative.
    """

    def resolve(metadata: dict[str, Any], call_context: Any) -> Principal:
        if authorizer is None:
            return default_principal
        token = metadata.get("token")
        if not token:
            raise Denied("no credential supplied with this A2A message")
        return authorizer.verify(token)

    return resolve


def build_agent_card(
    agent: DeepAgent,
    *,
    url: str,
    registry: AgentRegistry | None = None,
    version: str = "0.1.0",
) -> AgentCard:
    """The public descriptor served at ``/.well-known/agent-card.json``.

    Skills come from the registry when one is given — each registered
    ``AgentSpec`` becomes one discoverable A2A skill — or from the agent itself
    when it isn't, so a bare ``DeepAgent`` still serves a valid card.

    Note this advertises the JSON-RPC binding only. The app also serves the
    REST routes, but a card entry claims a transport a client may then pick,
    and the REST surface here has not been exercised end to end.
    """
    if registry is not None:
        skills = [
            AgentSkill(id=spec.name, name=spec.name, description=spec.description)
            for spec in registry.list()
        ]
    else:
        skills = [AgentSkill(id=agent.name, name=agent.name, description=agent.description)]

    return AgentCard(
        name=agent.name,
        description=agent.description,
        version=version,
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            )
        ],
        skills=skills,
    )


@dataclass
class DeepAgentExecutor(AgentExecutor):
    """Bridges the A2A protocol to one ``DeepAgent``.

    One executor per served agent — if a deployment wants two agents reachable
    over A2A, that's two executors behind two apps (different ports, or
    mounted at different paths), not one executor juggling both.

    Event choreography, and one deliberate change from the 0.3.x version:
    progress relays land as ``working`` status updates, files become artifacts
    in the order the agent wrote them, and **the answer text rides on the
    terminal event** rather than on a preceding ``working`` update. The old
    shape left a non-streaming ``message/send`` caller — who only sees the
    final Task — with an empty answer. Streaming callers see the same thing
    either way.
    """

    agent: DeepAgent
    resolve_principal: PrincipalResolver

    def _text_message(self, updater: TaskUpdater, text: str, *, progress: bool) -> Message:
        return updater.new_agent_message([Part(text=text, metadata={"is_progress": progress})])

    async def _emit_file(
        self, tag: str, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Enqueue one file artifact.

        Built and enqueued directly rather than via ``TaskUpdater.add_artifact``
        because that helper has no ``description`` parameter, and dropping the
        description would change what a caller sees on the wire.
        """
        attrs = dict(_TAG_ATTR.findall(tag))
        src = attrs.get("src", "")
        name = attrs.get("name") or Path(src).name
        desc = attrs.get("desc", "")
        if not src or not Path(src).is_file():
            log.warning("a2a.artifact_missing src=%r task=%s", src, context.task_id)
            return

        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                artifact=Artifact(
                    artifact_id=str(uuid.uuid4()),
                    name=name,
                    description=desc,
                    parts=[
                        Part(
                            # 1.x takes raw bytes; the 0.3.x base64-in-a-wrapper
                            # dance is gone.
                            raw=Path(src).read_bytes(),
                            filename=name,
                            media_type=_mime_type(name),
                            metadata={"description": desc, "is_progress": False},
                        )
                    ],
                ),
            )
        )

    async def _answer_text(
        self,
        response: AgentResponse,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> str:
        """Split file tags out of the answer, emitting each as an artifact.

        Returns the prose that is left, which the caller puts on the terminal
        event.
        """
        text = response.output or f"(no output, status={response.status})"
        prose: list[str] = []
        for chunk in RENDER_TAG.split(text):
            if not chunk.strip():
                continue
            if chunk.startswith("<render-cpochat"):
                await self._emit_file(chunk, context, event_queue)
            else:
                prose.append(chunk.strip())

        if response.citations:
            lines = [f"- [{c.kind}] {c.title or c.ref} ({c.ref})" for c in response.citations]
            prose.append("Sources:\n" + "\n".join(lines))

        return "\n\n".join(prose)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The first event MUST be a Task object. `DefaultRequestHandler` (which
        # is `DefaultRequestHandlerV2` under the alias) rejects a
        # TaskStatusUpdateEvent that arrives before one with "Agent should
        # enqueue Task before ...", and `TaskUpdater.submit()` emits a status
        # update, not a Task -- so submit() alone is not enough to open a task.
        # Every early return below is a status update too, hence this goes
        # first, before any of them.
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        task_text = context.get_user_input()
        if not task_text:
            await updater.failed(
                self._text_message(updater, "Error: no input provided", progress=False)
            )
            return

        metadata = _message_metadata(context.message)
        try:
            principal = self.resolve_principal(metadata, context.call_context)
        except Denied as exc:
            await updater.failed(
                self._text_message(
                    updater, f"Permission denied: {exc.reason}", progress=False
                )
            )
            return

        await updater.submit()
        await updater.start_work()

        inputs = metadata.get("inputs")
        request = AgentRequest(
            principal=principal,
            task=task_text,
            inputs=inputs if isinstance(inputs, dict) else {},
            trace_id=context.task_id,
        )

        response: AgentResponse | None = None
        try:
            async for event in self.agent.stream(request):
                if isinstance(event, AgentResponse):
                    response = event
                else:
                    await updater.update_status(
                        TaskState.TASK_STATE_WORKING,
                        self._text_message(updater, event[1], progress=True),
                    )
        except Exception as exc:  # surfaced to the caller, not swallowed
            log.exception("a2a.execute failed agent=%s task=%s", self.agent.name, context.task_id)
            await updater.failed(self._text_message(updater, str(exc), progress=False))
            return

        if response is None:
            await updater.failed(
                self._text_message(updater, "agent produced no response", progress=False)
            )
            return

        answer = await self._answer_text(response, context, event_queue)
        message = self._text_message(updater, answer, progress=False)
        if response.ok:
            await updater.complete(message)
        else:
            await updater.failed(message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel(self._text_message(updater, "Task canceled.", progress=False))


def build_a2a_app(
    agent: DeepAgent,
    *,
    url: str,
    registry: AgentRegistry | None = None,
    authorizer: Authorizer | None = None,
    default_principal: Principal | None = None,
    version: str = "0.1.0",
    lifespan: Any | None = None,
) -> FastAPI:
    """Wire one ``DeepAgent`` into a FastAPI app speaking A2A JSON-RPC + REST.

    Returns a ready-to-serve app. ``lifespan`` is passed through to ``FastAPI``
    for a caller that needs to start something alongside it — 1.x has no
    ``.build()`` step to hang that on, unlike the 0.3.x wrapper this replaced.
    """
    if default_principal is None:
        default_principal = Principal(
            subject="a2a:anonymous", division="shared", roles=frozenset({"wiki.reader"})
        )
    if authorizer is None:
        log.warning(
            "build_a2a_app(%s): no Authorizer configured -- every caller runs as "
            "principal=%r. Fine for local simulation, not for anything reachable "
            "outside this machine.",
            agent.name, default_principal.subject,
        )

    executor = DeepAgentExecutor(
        agent=agent,
        resolve_principal=_default_principal_resolver(authorizer, default_principal),
    )
    card = build_agent_card(agent, url=url, registry=registry, version=version)
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    app = FastAPI(title=f"{agent.name} (A2A)", description=agent.description, lifespan=lifespan)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        # enable_v0_3_compat accepts A2A v0.3-shaped requests, which is what a
        # caller sending no `A2A-Version` header is assumed to be sending (the
        # SDK defaults the absent header to "0.3"). Left on so 0.3 clients keep
        # working; it does not weaken the 1.0 path, which still requires the
        # header. See the module docstring's table.
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True),
        rest_routes=create_rest_routes(handler, enable_v0_3_compat=True),
    )
    return app


def serve(
    agent: DeepAgent,
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
    url: str | None = None,
    registry: AgentRegistry | None = None,
    authorizer: Authorizer | None = None,
    default_principal: Principal | None = None,
) -> None:
    """Blocking entry point: run ``uvicorn`` in the foreground.

    To run alongside the scheduler in one process, build the app with
    ``build_a2a_app`` and drive it with ``uvicorn.Server(...).serve()`` inside
    your own ``asyncio.gather`` instead — see ``deep_research_agent.serving.service``.
    """
    app = build_a2a_app(
        agent,
        url=url or f"http://{host}:{port}/",
        registry=registry,
        authorizer=authorizer,
        default_principal=default_principal,
    )
    uvicorn.run(app, host=host, port=port)
