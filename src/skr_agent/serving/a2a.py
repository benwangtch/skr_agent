"""Serve a ``DeepAgent`` as an A2A (Agent2Agent) server.

Written against ``a2a-sdk`` 1.1.x's actual installed API, not the older
convenience wrapper (``A2AStarletteApplication``) that most current blog posts
and tutorials still show — that class doesn't exist in this version. The
routes-based wiring here (``create_agent_card_routes`` /
``create_jsonrpc_routes`` / ``add_a2a_routes_to_fastapi``) and the request
handler's real name (``LegacyRequestHandler``, not ``DefaultRequestHandler``)
were both confirmed by importing and inspecting the installed package rather
than copied from a tutorial — pin ``a2a-sdk`` if you don't want a future
`pip install --upgrade` to move this out from under you again.

Two things this module deliberately does not build:

- **Streaming task events.** Every response here is enqueued as a single
  final message once the agent finishes. A2A supports incremental
  ``TaskArtifactUpdateEvent``s for long-running work; wiring that through
  would mean threading partial output out of ``DeepAgent.run()``, which
  doesn't currently exist. Worth doing before a UI wants to show progress on
  a multi-minute report; not needed for peer-agent-to-peer-agent calls, which
  are usually fine polling or just waiting.
- **Real caller authentication.** ``PrincipalResolver`` below is the seam —
  wire a real ``Authorizer`` (see ``protocol.py``) that verifies a bearer
  token before this reaches anything that isn't a fixture. Left unconfigured,
  every caller gets ``default_principal``, which is deliberately read-only and
  scoped to the shared namespace — loud enough to notice, not so open that a
  forgotten config accidentally exposes a write path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import uvicorn
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import PROTOCOL_VERSION_1_0, add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Part,
    TaskState,
)
from a2a.utils import TransportProtocol
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict

from ..mesh import AgentRegistry
from ..protocol import AgentRequest, Authorizer, Denied, Principal
from ..runtime import DeepAgent

log = logging.getLogger(__name__)

__all__ = ["build_agent_card", "DeepAgentExecutor", "build_a2a_app", "serve"]

PrincipalResolver = Callable[[dict[str, Any], ServerCallContext], Principal]


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
    this codebase: re-derive authority, don't trust caller-supplied claims.
    A message with no token, or a token that fails verification, is refused —
    it does not fall back to ``default_principal``, which would make the
    authorizer decorative.
    """

    def resolve(metadata: dict[str, Any], call_context: ServerCallContext) -> Principal:
        if authorizer is None:
            return default_principal
        token = metadata.get("token")
        if not token:
            raise Denied("no credential supplied with this A2A message")
        return authorizer.verify(token)

    return resolve


def _message_metadata(message: Message | None) -> dict[str, Any]:
    """``message.metadata`` is a protobuf ``Struct``, not a Python dict.

    Naive ``dict(message.metadata)`` converts only the top level — nested
    objects (like an ``inputs`` object, or anything but a flat string map)
    come back as unconverted protobuf ``Value``/``Struct`` wrappers, which
    then fail an ``isinstance(x, dict)`` check downstream and get silently
    dropped. ``MessageToDict`` recurses properly. Note the one thing it can't
    avoid: protobuf's JSON ``Value`` type has no integer type, so a caller
    sending ``{"inputs": {"count": 3}}`` gets ``3.0`` back, not ``3`` — accept
    floats on the receiving end for anything that arrives this way.
    """
    if message is None:
        return {}
    return MessageToDict(message.metadata)


def build_agent_card(
    agent: DeepAgent,
    *,
    base_url: str,
    registry: AgentRegistry | None = None,
    version: str = "0.1.0",
    streaming: bool = False,
) -> AgentCard:
    """The public descriptor served at ``/.well-known/agent-card.json``.

    Skills come from the registry when one is given — each registered
    ``AgentSpec`` becomes one discoverable A2A skill — or from the agent
    itself when it isn't, so a bare ``DeepAgent`` still serves a valid card.
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
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=streaming, push_notifications=False),
        supported_interfaces=[
            AgentInterface(
                url=base_url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_1_0,
            ),
        ],
        skills=skills,
    )


@dataclass
class DeepAgentExecutor(AgentExecutor):
    """Bridges the A2A protocol to one ``DeepAgent``.

    One executor per served agent — if a deployment wants both the report
    agent and copilot reachable over A2A, that's two ``DeepAgentExecutor``s
    behind two ``build_a2a_app`` calls (different ports, or mounted at
    different paths on one FastAPI app), not one executor juggling both.
    """

    agent: DeepAgent
    resolve_principal: PrincipalResolver

    async def execute(self, context, event_queue: EventQueue) -> None:  # noqa: ANN001
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.submit()
        await updater.start_work()

        task_text = context.get_user_input()
        metadata = _message_metadata(context.message)

        try:
            principal = self.resolve_principal(metadata, context.call_context)
        except Denied as exc:
            await updater.failed(
                message=updater.new_agent_message([Part(text=f"Permission denied: {exc.reason}")])
            )
            return

        inputs = metadata.get("inputs") or {}
        if not isinstance(inputs, dict):
            inputs = {}

        request = AgentRequest(
            principal=principal,
            task=task_text,
            inputs=inputs,
            trace_id=context.task_id,
        )

        try:
            response = await self.agent.run(request)
        except Exception as exc:  # surfaced to the caller, not swallowed
            log.exception("a2a.execute failed agent=%s task=%s", self.agent.name, context.task_id)
            await updater.failed(message=updater.new_agent_message([Part(text=str(exc))]))
            return

        parts = [Part(text=response.output or f"(no output, status={response.status})")]
        if response.citations:
            lines = [f"- [{c.kind}] {c.title or c.ref} ({c.ref})" for c in response.citations]
            parts.append(Part(text="Sources:\n" + "\n".join(lines)))

        if response.ok:
            await updater.complete(message=updater.new_agent_message(parts))
        else:
            await updater.failed(message=updater.new_agent_message(parts))

    async def cancel(self, context, event_queue: EventQueue) -> None:  # noqa: ANN001
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.update_status(TaskState.TASK_STATE_CANCELED)


def build_a2a_app(
    agent: DeepAgent,
    *,
    base_url: str,
    registry: AgentRegistry | None = None,
    authorizer: Authorizer | None = None,
    default_principal: Principal | None = None,
) -> FastAPI:
    """Wire one ``DeepAgent`` into a FastAPI app speaking A2A JSON-RPC + REST."""
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

    resolver = _default_principal_resolver(authorizer, default_principal)
    executor = DeepAgentExecutor(agent=agent, resolve_principal=resolver)
    card = build_agent_card(agent, base_url=base_url, registry=registry)
    handler = LegacyRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    app = FastAPI(title=f"{agent.name} (A2A)", description=agent.description)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        # enable_v0_3_compat widens which A2A protocol v0.3 request shapes
        # this accepts. It does NOT relax the version check below -- every
        # caller must still send the `A2A-Version: 1.0` header (verified:
        # requests without it are rejected with VERSION_NOT_SUPPORTED
        # regardless of this flag). Left on because it's a strict widening
        # for callers that do send the header with an older body shape.
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True),
        rest_routes=create_rest_routes(handler, enable_v0_3_compat=True),
    )
    return app


def serve(
    agent: DeepAgent,
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
    base_url: str | None = None,
    registry: AgentRegistry | None = None,
    authorizer: Authorizer | None = None,
    default_principal: Principal | None = None,
) -> None:
    """Blocking entry point: run ``uvicorn`` in the foreground.

    For running alongside a scheduler in the same process, build the app with
    ``build_a2a_app`` and drive it with ``uvicorn.Server(...).serve()`` inside
    your own ``asyncio.gather`` instead of calling this — see
    ``skr_agent.serving.service``.
    """
    app = build_a2a_app(
        agent,
        base_url=base_url or f"http://{host}:{port}/",
        registry=registry,
        authorizer=authorizer,
        default_principal=default_principal,
    )
    uvicorn.run(app, host=host, port=port)
