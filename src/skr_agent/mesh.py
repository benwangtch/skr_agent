"""Turning agents into tools, and keeping track of who exists.

This is the piece that makes "copilot calls the wiki coordinator" and "the
report agent calls the wiki coordinator" the same mechanism rather than two
integrations. It is also the seed of the skills-sharing platform: a registry of
named capabilities that can be listed, described, and handed to a model.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable

from claude_agent_sdk import create_sdk_mcp_server, tool

from .protocol import (
    AgentRequest,
    AgentResponse,
    AgentSpec,
    Denied,
    Principal,
)

log = logging.getLogger(__name__)

__all__ = ["AgentRegistry", "agent_as_tool", "agents_as_toolserver"]


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
# Agent -> SDK tool
# --------------------------------------------------------------------------


def _render(response: AgentResponse) -> dict[str, Any]:
    """Format an AgentResponse as MCP tool output.

    Citations are rendered inline rather than dropped, because the calling
    model needs them to attribute its own answer. A refusal is returned as
    ordinary text with ``is_error`` set — the caller should see *why* it was
    refused and adapt, not treat it as a transport failure.
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

    block: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if not response.ok:
        block["is_error"] = True
    return block


def agent_as_tool(
    spec: AgentSpec,
    *,
    principal: Principal,
    parent: str,
    max_turns: int | None = None,
    on_response: Callable[[AgentResponse], None] | None = None,
):
    """Expose an agent to a *calling* model as a single tool.

    The principal is closed over at construction time rather than passed as a
    tool argument. That is the whole trick: a model cannot escalate by writing a
    different ``division`` into the tool call, because the field does not exist
    in the schema. Tools are therefore built per request, not once at import.
    """

    async def _invoke(args: dict[str, Any]) -> dict[str, Any]:
        task = args.get("task", "")
        inputs = {k: v for k, v in args.items() if k != "task"}
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

    _invoke.__name__ = spec.name
    return tool(spec.name, spec.description, spec.input_schema)(_invoke)


def agents_as_toolserver(
    specs: Iterable[AgentSpec],
    *,
    principal: Principal,
    parent: str,
    server_name: str = "agents",
    on_response: Callable[[AgentResponse], None] | None = None,
):
    """Bundle several agents into one in-process MCP server.

    Returns ``(server_config, allowed_tool_names)`` so the caller can pass both
    straight into ``ClaudeAgentOptions``.
    """
    specs = list(specs)
    tools = [
        agent_as_tool(s, principal=principal, parent=parent, on_response=on_response)
        for s in specs
    ]
    server = create_sdk_mcp_server(name=server_name, version="1.0.0", tools=tools)
    names = [f"mcp__{server_name}__{s.name}" for s in specs]
    return server, names
