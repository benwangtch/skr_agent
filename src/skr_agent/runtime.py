"""A thin, reusable deep-agent runtime on top of the Claude Agent SDK.

Everything feature-specific — which tools exist, what the prompt says, which
subagents to spawn — is injected. What lives here is the part every agent in the
mesh would otherwise reimplement: binding tools to a principal, enforcing a
budget, collecting citations, and turning a stream of SDK messages back into an
``AgentResponse``.

The SDK already supplies the agent loop, planning, context compaction, subagent
dispatch, and skill loading. This module does not re-implement any of that; it
adapts it to the mesh contract.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .config import get_llm
from .protocol import (
    AgentRequest,
    AgentResponse,
    AgentSpec,
    Citation,
    Denied,
    Principal,
    Usage,
)

log = logging.getLogger(__name__)

__all__ = ["ToolContext", "ToolBundle", "DeepAgent"]


@dataclass
class ToolContext:
    """Handed to every toolset factory when a request starts.

    Tools receive the principal here rather than as a tool argument, so a model
    cannot claim a different identity than the one it is running under. They
    also get a citation sink: a tool that reads a wiki page or fetches an
    article records where the content came from, so the runtime can attach
    provenance to the final answer without the model having to remember to.
    """

    principal: Principal
    request: AgentRequest
    citations: list[Citation] = field(default_factory=list)

    def cite(self, citation: Citation) -> None:
        if citation not in self.citations:
            self.citations.append(citation)


ToolBundle = tuple[Any, Sequence[str]]
"""``(mcp_server_config, allowed_tool_names)`` — what ClaudeAgentOptions wants."""

ToolsetFactory = Callable[[ToolContext], ToolBundle]


_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull a trailing fenced JSON block out of the agent's final message.

    Used when a caller wants structured output alongside prose. We take the
    *last* block, because a deep agent often shows intermediate JSON while
    working and the final one is the answer.
    """
    matches = _JSON_BLOCK.findall(text)
    for raw in reversed(matches):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        return {"items": parsed}
    return {}


class DeepAgent:
    """An agent that plans, uses tools, and may delegate to subagents.

    Reach for this when the number of steps is not known in advance — when the
    model has to decide how much digging is enough. For fixed pipelines, call
    the Messages API directly instead; a deep agent is the expensive option and
    should be spent where the open-endedness is real.
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        system_prompt: str,
        toolsets: Iterable[ToolsetFactory] = (),
        subagents: dict[str, AgentDefinition] | None = None,
        skills: list[str] | None = None,
        builtin_tools: Sequence[str] = (),
        model: str | None = None,
        effort: str | None = None,
        max_turns: int = 40,
        cwd: str | None = None,
        setting_sources: list[str] | None = None,
        input_schema: dict[str, Any] | None = None,
        structured_output: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.toolsets = list(toolsets)
        self.subagents = subagents or {}
        self.skills = skills
        self.builtin_tools = list(builtin_tools)
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.cwd = cwd
        # Default to loading nothing from disk. Agents that ship skills opt
        # into ["project"], which is how the SDK discovers .claude/skills.
        self.setting_sources = [] if setting_sources is None else setting_sources
        self.structured_output = structured_output
        self._input_schema = input_schema

    # -- mesh integration ---------------------------------------------------

    def as_spec(self) -> AgentSpec:
        """Expose this agent to the registry, and thus to other agents."""
        schema = self._input_schema or {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What you want this agent to do, in full sentences.",
                }
            },
            "required": ["task"],
        }
        return AgentSpec(
            name=self.name,
            description=self.description,
            handler=self.run,
            input_schema=schema,
        )

    # -- execution ----------------------------------------------------------

    def _build_options(self, ctx: ToolContext) -> ClaudeAgentOptions:
        servers: dict[str, Any] = {}
        allowed: list[str] = list(self.builtin_tools)

        for i, factory in enumerate(self.toolsets):
            server, names = factory(ctx)
            servers[f"ts{i}"] = server
            allowed.extend(names)

        if self.skills:
            allowed.append("Skill")

        budget = ctx.request.budget
        llm = get_llm()
        options = ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            mcp_servers=servers,
            allowed_tools=allowed,
            agents=self.subagents or None,
            skills=self.skills,
            model=self.model or llm.resolved_model(),
            effort=self.effort or llm.effort,
            max_turns=min(self.max_turns, budget.max_turns),
            permission_mode="bypassPermissions",
            # Tools come from this process unless an agent explicitly opts in.
            # Left at the default, the SDK would also load ~/.claude and local
            # settings, silently widening a tool surface an operator thought
            # they had pinned down.
            setting_sources=self.setting_sources,  # type: ignore[arg-type]
            # This is the actual environment swap: which endpoint and
            # credential the CLI subprocess for *this turn* talks to. See
            # config/llm.py::to_cli_env for why provider="openrouter"/"custom"
            # sends an empty ANTHROPIC_API_KEY rather than omitting it.
            env=llm.to_cli_env(),
        )
        if self.cwd:
            options.cwd = self.cwd
        if budget.max_usd is not None:
            options.max_budget_usd = budget.max_usd
        return options

    def _prompt(self, request: AgentRequest) -> str:
        parts = [request.task]
        if request.inputs:
            parts.append(
                "Structured inputs:\n```json\n"
                + json.dumps(request.inputs, indent=2, default=str)
                + "\n```"
            )
        if self.structured_output:
            parts.append(
                "End your final message with a single ```json fenced block "
                "containing the structured result."
            )
        return "\n\n".join(parts)

    async def run(self, request: AgentRequest) -> AgentResponse:
        if request.budget.expired():
            return AgentResponse.fail(
                "budget_exhausted", "deadline passed before work started",
                trace_id=request.trace_id,
            )

        ctx = ToolContext(principal=request.principal, request=request)
        try:
            options = self._build_options(ctx)
        except Denied as exc:
            return AgentResponse.refuse(exc.reason, trace_id=request.trace_id)

        text_parts: list[str] = []
        usage = Usage()
        status: str = "ok"

        try:
            async for message in query(prompt=self._prompt(request), options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    usage = _usage_from_result(message)
                    if getattr(message, "is_error", False):
                        status = "partial"
        except Exception as exc:
            log.exception("runtime.failed agent=%s trace=%s", self.name, request.trace_id)
            return AgentResponse.fail("agent_error", str(exc), trace_id=request.trace_id)

        text = "\n".join(p for p in text_parts if p).strip()
        data = _extract_json(text) if self.structured_output else {}

        if usage.turns and usage.turns >= options.max_turns:
            # The loop was cut off rather than finishing — say so instead of
            # presenting a truncated answer as complete.
            status = "partial"

        return AgentResponse(
            status=status,  # type: ignore[arg-type]
            output=text,
            data=data,
            citations=tuple(ctx.citations),
            usage=usage,
            trace_id=request.trace_id,
        )


def _usage_from_result(message: ResultMessage) -> Usage:
    raw = getattr(message, "usage", None) or {}
    if not isinstance(raw, dict):
        raw = {}
    return Usage(
        turns=getattr(message, "num_turns", 0) or 0,
        input_tokens=raw.get("input_tokens", 0) or 0,
        output_tokens=raw.get("output_tokens", 0) or 0,
        cost_usd=getattr(message, "total_cost_usd", 0.0) or 0.0,
    )
