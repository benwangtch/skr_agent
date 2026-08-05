"""Serve a ``DeepAgent`` as an A2A (Agent2Agent) server.

Written against ``a2a-sdk`` 0.3.x — the generation the team's own reference
server uses (``A2AStarletteApplication`` + ``DefaultRequestHandler``). The
1.x line renames most of this (``LegacyRequestHandler``, routes-based wiring,
protobuf message types, a mandatory ``A2A-Version`` header) and is *not*
compatible; the pin in ``pyproject.toml`` is deliberate, not incidental.

Two things this module adds over a minimal executor:

- **Progress streaming.** A BOM sweep takes minutes. Every step the agent
  takes is relayed as a non-final ``TaskStatusUpdateEvent`` so a caller sees
  motion instead of a silent connection, then one final ``completed`` event.
- **Real caller authentication, as a seam.** ``PrincipalResolver`` is where a
  real ``Authorizer`` (see ``protocol.py``) verifies a bearer token. Left
  unconfigured, every caller gets ``default_principal``, which is deliberately
  read-only and scoped to the shared namespace — loud enough to notice, not so
  open that a forgotten config accidentally exposes a write path.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    FilePart,
    FileWithBytes,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

from ..mesh import AgentRegistry
from ..protocol import AgentRequest, AgentResponse, Authorizer, Denied, Principal
from ..runtime import DeepAgent

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
    """
    if registry is not None:
        skills = [
            AgentSkill(id=spec.name, name=spec.name, description=spec.description, tags=[])
            for spec in registry.list()
        ]
    else:
        skills = [
            AgentSkill(id=agent.name, name=agent.name, description=agent.description, tags=[])
        ]

    return AgentCard(
        name=agent.name,
        description=agent.description,
        url=url,
        version=version,
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=skills,
    )


@dataclass
class DeepAgentExecutor(AgentExecutor):
    """Bridges the A2A protocol to one ``DeepAgent``.

    One executor per served agent — if a deployment wants two agents reachable
    over A2A, that's two executors behind two apps (different ports, or
    mounted at different paths), not one executor juggling both.
    """

    agent: DeepAgent
    resolve_principal: PrincipalResolver

    def _message(self, text: str, context: RequestContext, *, progress: bool) -> Message:
        return Message(
            role=Role.agent,
            parts=[Part(root=TextPart(text=text, metadata={"is_progress": progress}))],
            message_id=str(uuid.uuid4()),
            task_id=context.task_id,
            context_id=context.context_id,
        )

    async def _emit_text(
        self, text: str, context: RequestContext, event_queue: EventQueue, *, progress: bool
    ) -> None:
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                final=False,
                status=TaskStatus(
                    state=TaskState.working,
                    message=self._message(text, context, progress=progress),
                ),
            )
        )

    async def _emit_file(
        self, tag: str, context: RequestContext, event_queue: EventQueue
    ) -> None:
        attrs = dict(_TAG_ATTR.findall(tag))
        src = attrs.get("src", "")
        name = attrs.get("name") or Path(src).name
        desc = attrs.get("desc", "")
        if not src or not Path(src).is_file():
            log.warning("a2a.artifact_missing src=%r task=%s", src, context.task_id)
            return

        payload = base64.b64encode(Path(src).read_bytes()).decode("utf-8")
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
                            root=FilePart(
                                file=FileWithBytes(
                                    bytes=payload, name=name, mime_type=_mime_type(name)
                                ),
                                metadata={"description": desc, "is_progress": False},
                            )
                        )
                    ],
                ),
            )
        )

    async def _emit_answer(
        self, response: AgentResponse, context: RequestContext, event_queue: EventQueue
    ) -> None:
        """Emit the final answer, splitting out any file-render tags.

        Text and files interleave in the order the agent wrote them, so a
        report that references a chart before explaining it still reads in
        that order on the other end.
        """
        text = response.output or f"(no output, status={response.status})"
        for chunk in RENDER_TAG.split(text):
            if not chunk.strip():
                continue
            if chunk.startswith("<render-cpochat"):
                await self._emit_file(chunk, context, event_queue)
            else:
                await self._emit_text(chunk, context, event_queue, progress=False)

        if response.citations:
            lines = [f"- [{c.kind}] {c.title or c.ref} ({c.ref})" for c in response.citations]
            await self._emit_text(
                "Sources:\n" + "\n".join(lines), context, event_queue, progress=False
            )

    async def _finish(
        self, context: RequestContext, event_queue: EventQueue, state: TaskState,
        text: str | None = None,
    ) -> None:
        status = TaskStatus(state=state)
        if text is not None:
            status = TaskStatus(
                state=state, message=self._message(text, context, progress=False)
            )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                final=True,
                status=status,
            )
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_text = context.get_user_input()
        if not task_text:
            await self._finish(
                context, event_queue, TaskState.failed, "Error: no input provided"
            )
            return

        metadata = (context.message.metadata if context.message else None) or {}
        try:
            principal = self.resolve_principal(metadata, context.call_context)
        except Denied as exc:
            await self._finish(
                context, event_queue, TaskState.failed, f"Permission denied: {exc.reason}"
            )
            return

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
                    await self._emit_text(event[1], context, event_queue, progress=True)
        except Exception as exc:  # surfaced to the caller, not swallowed
            log.exception("a2a.execute failed agent=%s task=%s", self.agent.name, context.task_id)
            await self._finish(context, event_queue, TaskState.failed, str(exc))
            return

        if response is None:
            await self._finish(
                context, event_queue, TaskState.failed, "agent produced no response"
            )
            return

        await self._emit_answer(response, context, event_queue)
        await self._finish(
            context, event_queue,
            TaskState.completed if response.ok else TaskState.failed,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await self._finish(context, event_queue, TaskState.canceled, "Task canceled.")


def build_a2a_app(
    agent: DeepAgent,
    *,
    url: str,
    registry: AgentRegistry | None = None,
    authorizer: Authorizer | None = None,
    default_principal: Principal | None = None,
    version: str = "0.1.0",
) -> A2AStarletteApplication:
    """Wire one ``DeepAgent`` into an A2A application.

    Returns the ``A2AStarletteApplication`` rather than a built app so a caller
    can pass its own ``lifespan`` to ``.build()`` — which is how the process
    that also runs the scheduler starts both together.
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
    return A2AStarletteApplication(
        agent_card=build_agent_card(agent, url=url, registry=registry, version=version),
        http_handler=DefaultRequestHandler(
            agent_executor=executor, task_store=InMemoryTaskStore()
        ),
    )


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
    your own ``asyncio.gather`` instead — see ``skr_agent.serving.service``.
    """
    a2a_app = build_a2a_app(
        agent,
        url=url or f"http://{host}:{port}/",
        registry=registry,
        authorizer=authorizer,
        default_principal=default_principal,
    )
    uvicorn.run(a2a_app.build(), host=host, port=port)
