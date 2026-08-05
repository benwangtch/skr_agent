"""Turning agents into tools, and keeping track of who exists.

This is what lets one agent be handed to another as a single tool rather than
as a bespoke integration. It is also the seed of the skills-sharing platform: a
registry of named capabilities that can be listed, described, and handed to a
model.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable

from langchain_core.tools import BaseTool, StructuredTool

from .protocol import (
    AgentRequest,
    AgentResponse,
    AgentSpec,
    Denied,
    Principal,
)

log = logging.getLogger(__name__)

__all__ = ["AgentRegistry", "agent_as_tool", "agents_as_tools"]


class AgentRegistry:
    """An in-process directory of agents.

    Deliberately not a service. The point of registering is that a caller can
    discover capabilities by name and description instead of importing a
    concrete class — which is what a marketplace needs later, and costs nothing
    now.
    """

    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> AgentSpec:
        if spec.name in self._specs:
            raise ValueError(f"agent {spec.name!r} already registered")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> AgentSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise KeyError(f"no agent named {name!r}") from None

    def list(self, *, tag: str | None = None) -> list[AgentSpec]:
        specs = self._specs.values()
        if tag is not None:
            specs = [s for s in specs if tag in s.tags]
        return sorted(specs, key=lambda s: s.name)

    def catalog(self) -> str:
        """A human/model-readable listing. What a marketplace UI renders."""
        return "\n".join(f"- {s.name}: {s.description}" for s in self.list())

    async def dispatch(self, name: str, request: AgentRequest) -> AgentResponse:
        return await self.get(name)(request)


# --------------------------------------------------------------------------
# Agent -> tool
# --------------------------------------------------------------------------


def _render(response: AgentResponse) -> str:
    """Format an AgentResponse as tool output.

    Citations are rendered inline rather than dropped, because the calling
    model needs them to attribute its own answer. A refusal is returned as
    ordinary text — the caller should see *why* it was refused and adapt, not
    treat it as a transport failure.
    """
    parts: list[str] = []
    if response.output:
        parts.append(response.output)
    if response.data:
        parts.append("```json\n" + json.dumps(response.data, indent=2, default=str) + "\n```")
    if response.citations:
        lines = ["Sources:"]
        for c in response.citations:
            label = c.title or c.ref
            lines.append(f"- [{c.kind}] {label} ({c.ref})")
        parts.append("\n".join(lines))
    text = "\n\n".join(parts) or f"(no output, status={response.status})"
    if not response.ok:
        text = f"[{response.status}] {text}"
    return text


def agent_as_tool(
    spec: AgentSpec,
    *,
    principal: Principal,
    parent: str,
    max_turns: int | None = None,
    on_response: Callable[[AgentResponse], None] | None = None,
) -> BaseTool:
    """Expose an agent to a *calling* model as a single tool.

    The principal is closed over at construction time rather than passed as a
    tool argument. That is the whole trick: a model cannot escalate by writing
    a different ``division`` into the tool call, because the field does not
    exist in the schema. Tools are therefore built per request, not once at
    import.
    """

    async def _invoke(**kwargs: Any) -> str:
        task = kwargs.get("task", "")
        inputs = {k: v for k, v in kwargs.items() if k != "task" and v is not None}
        request = AgentRequest(
            principal=principal,
            task=task,
            inputs=inputs,
            parent_agent=parent,
        )
        if max_turns is not None:
            request = request.delegate(task, agent=parent, inputs=inputs, max_turns=max_turns)
        try:
            response = await spec(request)
        except Denied as exc:
            log.info("mesh.denied agent=%s subject=%s", spec.name, principal.subject)
            response = AgentResponse.refuse(exc.reason, trace_id=request.trace_id)
        except Exception as exc:  # surfaced to the model, not swallowed
            log.exception("mesh.error agent=%s", spec.name)
            response = AgentResponse.fail("agent_error", str(exc), trace_id=request.trace_id)
        if on_response is not None:
            # Lets the caller harvest citations from a delegated call, so
            # provenance survives the hop instead of only reaching the model
            # as prose it may or may not repeat.
            on_response(response)
        return _render(response)

    return StructuredTool.from_function(
        coroutine=_invoke,
        name=spec.name,
        description=spec.description,
        args_schema=spec.input_schema,
    )


def agents_as_tools(
    specs: Iterable[AgentSpec],
    *,
    principal: Principal,
    parent: str,
    on_response: Callable[[AgentResponse], None] | None = None,
) -> list[BaseTool]:
    """Bundle several agents into a list of tools ready to hand to an agent."""
    return [
        agent_as_tool(s, principal=principal, parent=parent, on_response=on_response)
        for s in specs
    ]
