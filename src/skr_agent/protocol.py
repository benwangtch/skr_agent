"""The wire contract every agent in the mesh speaks.

This module deliberately knows nothing about wiki, reports, or Claude. It is the
one piece skr agent, its data sources, its serving layer, and (later) the
skills-sharing platform all depend on, so it must stay free of feature
concepts — and it is what let the agent framework underneath be swapped
without any of them changing.

Two ideas carry most of the weight:

``Principal`` is *authority* — who is asking, and what attributes an authorizer
can decide against. It travels with every call and is never inferred from the
caller's own claims about what it is allowed to do.

``AgentSpec`` is *capability* — a callable unit of work with a name, a
description, and a JSON schema. An agent, a tool, and a skill-backed workflow are
all the same shape here, which is what lets one agent be handed to another as a
tool without a bespoke adapter each time.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Literal, Protocol

__all__ = [
    "Principal",
    "Budget",
    "AgentRequest",
    "AgentResponse",
    "Citation",
    "Usage",
    "AgentError",
    "AgentSpec",
    "Authorizer",
    "Denied",
    "new_trace_id",
]


def new_trace_id() -> str:
    return f"trc_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------
# Authority
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is asking.

    ``token`` is the only field an authorizer should ultimately trust. The
    others are a decoded convenience view of it, carried so that in-process
    callers do not have to re-parse on every hop. A service that receives a
    Principal from outside its own trust boundary re-verifies ``token`` and
    rebuilds the rest — see ``Authorizer``.
    """

    subject: str
    """Stable user id."""

    division: str
    """The org unit the user belongs to, e.g. ``"platform"``. Drives wiki
    namespace access, but the mapping itself lives with the wiki service."""

    roles: frozenset[str] = frozenset()
    """Coarse role grants, e.g. ``{"wiki.reader", "wiki.writer"}``."""

    attributes: dict[str, str] = field(default_factory=dict)
    """Free-form claims for authorizers that need more than division + roles."""

    token: str | None = None
    """The verifiable credential this Principal was decoded from. ``None``
    means "trusted in-process construction" — acceptable for tests and for the
    an in-process boot path, never for a request that crossed a network."""

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def redacted(self) -> "Principal":
        """A copy safe to put in logs and traces."""
        return replace(self, token=None, attributes={})


class Denied(Exception):
    """Raised by an ``Authorizer`` when a principal may not do something.

    Callers convert this into an ``AgentResponse`` with ``status="refused"``
    rather than letting it escape as a crash — a refusal is a normal outcome,
    not a bug.
    """

    def __init__(self, reason: str, *, subject: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.subject = subject


class Authorizer(Protocol):
    """Each service brings its own.

    The mesh does not centralize authorization: the wiki service is the only
    thing that knows how divisions map to namespaces, and the report service is
    the only thing that knows which BOMs a user may scan. Centralizing that
    knowledge would mean every new feature edits a shared policy file.

    What the mesh *does* standardize is that the check happens at all, and that
    it happens against a re-verified principal rather than a caller assertion.
    """

    def verify(self, token: str) -> Principal:
        """Decode and validate a credential into a Principal.

        Raise ``Denied`` if the token is absent, malformed, or expired.
        """
        ...


# --------------------------------------------------------------------------
# Request / response
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budget:
    """A ceiling the callee is expected to respect and report against."""

    max_turns: int = 40
    max_usd: float | None = None
    deadline_ts: float | None = None

    def expired(self) -> bool:
        return self.deadline_ts is not None and time.time() > self.deadline_ts

    def child(self, *, max_turns: int | None = None) -> "Budget":
        """A budget for delegated work — never larger than the parent's."""
        return Budget(
            max_turns=min(max_turns or self.max_turns, self.max_turns),
            max_usd=self.max_usd,
            deadline_ts=self.deadline_ts,
        )


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """One unit of work handed to one agent."""

    principal: Principal
    task: str
    """Natural language. Agents in this mesh are prompted, not RPC'd — the
    structured part of the ask goes in ``inputs``."""

    inputs: dict[str, Any] = field(default_factory=dict)
    budget: Budget = field(default_factory=Budget)
    trace_id: str = field(default_factory=new_trace_id)
    parent_agent: str | None = None
    """Set when this request was issued by another agent rather than a human.
    Used for loop detection and for reading traces, not for authorization."""

    def delegate(
        self,
        task: str,
        *,
        agent: str,
        inputs: dict[str, Any] | None = None,
        max_turns: int | None = None,
    ) -> "AgentRequest":
        """Derive a sub-request. Principal and trace carry over; budget shrinks."""
        return AgentRequest(
            principal=self.principal,
            task=task,
            inputs=inputs or {},
            budget=self.budget.child(max_turns=max_turns),
            trace_id=self.trace_id,
            parent_agent=agent,
        )


@dataclass(frozen=True, slots=True)
class Citation:
    """Where a claim came from.

    Every factual statement an agent returns should be traceable to one of
    these. The wiki feature's whole value proposition is that a wiki page points
    back at the raw weekly report it was distilled from, so citations are part
    of the contract rather than a presentation detail.
    """

    kind: Literal["wiki_page", "raw_report", "external_url", "internal_record"]
    ref: str
    title: str = ""
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class Usage:
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class AgentError:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """What every agent returns, whether it was called by a human or a peer."""

    status: Literal["ok", "partial", "refused", "failed"]
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    citations: tuple[Citation, ...] = ()
    usage: Usage = field(default_factory=Usage)
    trace_id: str = ""
    error: AgentError | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "partial")

    @classmethod
    def refuse(cls, reason: str, *, trace_id: str = "") -> "AgentResponse":
        return cls(
            status="refused",
            output=reason,
            trace_id=trace_id,
            error=AgentError("permission_denied", reason),
        )

    @classmethod
    def fail(cls, code: str, message: str, *, trace_id: str = "") -> "AgentResponse":
        return cls(
            status="failed",
            output=message,
            trace_id=trace_id,
            error=AgentError(code, message),
        )


# --------------------------------------------------------------------------
# Capability
# --------------------------------------------------------------------------

Handler = Callable[[AgentRequest], Awaitable[AgentResponse]]


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """A named, described, schema'd unit of work.

    The description is not documentation — it is the prompt fragment another
    agent reads when deciding whether to call this. Say *when* to reach for it,
    not just what it does.
    """

    name: str
    description: str
    handler: Handler
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
        }
    )
    tags: frozenset[str] = frozenset()

    async def __call__(self, request: AgentRequest) -> AgentResponse:
        return await self.handler(request)
