"""The skr agent runtime: a deep research agent on LangChain's ``deepagents``.

Everything feature-specific — which tools exist, what the prompt says, which
subagents to spawn — is injected. What lives here is the part every agent
would otherwise reimplement: binding tools to a principal, enforcing a budget,
collecting citations, and turning a LangGraph run back into an
``AgentResponse``.

``deepagents`` supplies the agent loop, planning/todo state, context
summarization, a virtual filesystem, and subagent dispatch. This module does
not re-implement any of that; it adapts it to the mesh contract in
``protocol.py``, which stays framework-agnostic so the harness underneath can
be replaced again without touching callers.

One deliberate departure from the framework's defaults: **skills are inlined
into the system prompt, not loaded through ``create_deep_agent(skills=...)``.**
That parameter implements progressive disclosure — the model is shown a
skill's name and description and is expected to call ``read_file`` to fetch
the body. For a rubric the agent must follow on every run, "the model
remembers to go read it" is exactly the failure mode worth engineering out, so
``skills=`` here means "read these SKILL.md files and put them in the prompt".
Progressive disclosure is the better trade once there are many optional
skills; it is the wrong one for a mandatory format spec.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool

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

__all__ = ["ToolContext", "ToolBundle", "ToolsetFactory", "SubagentFactory", "DeepAgent"]


@dataclass
class ToolContext:
    """Handed to every toolset factory when a request starts.

    Tools receive the principal here rather than as a tool argument, so a
    model cannot claim a different identity than the one it is running under.
    They also get a citation sink: a tool that reads a record or fetches an
    article records where the content came from, so the runtime can attach
    provenance to the final answer without the model having to remember to.
    """

    principal: Principal
    request: AgentRequest
    citations: list[Citation] = field(default_factory=list)

    def cite(self, citation: Citation) -> None:
        if citation not in self.citations:
            self.citations.append(citation)


ToolBundle = Sequence[BaseTool]
"""What a toolset factory returns: LangChain tools bound to one principal."""

ToolsetFactory = Callable[[ToolContext], ToolBundle]

SubagentFactory = Callable[[ToolContext, dict[str, BaseTool]], Sequence[dict[str, Any]]]
"""Builds ``deepagents.SubAgent`` specs. Takes the already-built tools by name
so a subagent can be given a strict subset — a read-only investigator is one
that simply never receives the write tool."""


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


def _message_text(message: BaseMessage) -> str:
    """Flatten a message's content to text.

    Content is a plain string on most providers but a list of typed blocks on
    others (Anthropic, and OpenAI in reasoning mode). Reading only ``.text``
    on the string case and ignoring the list case silently loses the answer on
    exactly the providers this repo is most likely to be pointed at.
    """
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def load_skill(root: str | Path, name: str) -> str:
    """Read one ``.claude/skills/<name>/SKILL.md`` body, minus its frontmatter.

    The YAML frontmatter is metadata for a skill *catalog*; inlining it into a
    prompt would just spend tokens telling the model a description of
    instructions that are already right there.
    """
    path = Path(root) / ".claude" / "skills" / name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text.strip()


class DeepAgent:
    """An agent that plans, uses tools, and may delegate to subagents.

    Reach for this when the number of steps is not known in advance — when the
    model has to decide how much digging is enough. For fixed pipelines, call
    a chat model directly instead; a deep agent is the expensive option and
    should be spent where the open-endedness is real.
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        system_prompt: str,
        toolsets: Iterable[ToolsetFactory] = (),
        subagents: SubagentFactory | None = None,
        skills: list[str] | None = None,
        project_root: str | Path | None = None,
        model: str | None = None,
        max_turns: int = 40,
        input_schema: dict[str, Any] | None = None,
        structured_output: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.toolsets = list(toolsets)
        self.subagents = subagents
        self.skills = skills or []
        self.project_root = project_root
        self.model = model
        self.max_turns = max_turns
        self.structured_output = structured_output
        self._input_schema = input_schema

        if self.skills and project_root is None:
            raise ValueError(f"agent {name!r}: skills require project_root")

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

    def _full_system_prompt(self) -> str:
        parts = [self.system_prompt]
        for skill in self.skills:
            body = load_skill(self.project_root or ".", skill)
            parts.append(f"# Skill: {skill}\n\n{body}")
        return "\n\n".join(parts)

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

    def build_tools(self, ctx: ToolContext) -> tuple[list[BaseTool], list[dict[str, Any]] | None]:
        """Resolve this agent's tool surface for one request.

        Public because it is the whole tool surface a run will have, and
        checking it is how a test catches "this agent can write when it
        shouldn't" without spending a paid model call to find out.
        """
        tools: list[BaseTool] = []
        for factory in self.toolsets:
            tools.extend(factory(ctx))

        subagents = None
        if self.subagents is not None:
            by_name = {t.name: t for t in tools}
            subagents = list(self.subagents(ctx, by_name))
        return tools, subagents

    def _build_graph(self, ctx: ToolContext):
        from deepagents import create_deep_agent

        tools, subagents = self.build_tools(ctx)
        return create_deep_agent(
            model=get_llm().build_chat_model(model=self.model),
            tools=tools,
            system_prompt=self._full_system_prompt(),
            subagents=subagents,
        )

    def _response(
        self, messages: Sequence[BaseMessage], ctx: ToolContext, request: AgentRequest
    ) -> AgentResponse:
        text = ""
        for message in reversed(messages):
            if isinstance(message, AIMessage) and not message.tool_calls:
                text = _message_text(message).strip()
                break
        return AgentResponse(
            status="ok",
            output=text,
            data=_extract_json(text) if self.structured_output else {},
            citations=tuple(ctx.citations),
            usage=_usage_from_messages(messages),
            trace_id=request.trace_id,
        )

    def _recursion_limit(self, request: AgentRequest) -> int:
        # LangGraph counts every node step, not every model turn. A deep-agent
        # turn is roughly model -> tools -> model, so the ceiling has to be a
        # multiple of max_turns or a run well inside its turn budget dies early
        # with GraphRecursionError.
        return min(self.max_turns, request.budget.max_turns) * 3

    async def run(self, request: AgentRequest) -> AgentResponse:
        if request.budget.expired():
            return AgentResponse.fail(
                "budget_exhausted", "deadline passed before work started",
                trace_id=request.trace_id,
            )

        ctx = ToolContext(principal=request.principal, request=request)
        try:
            graph = self._build_graph(ctx)
        except Denied as exc:
            return AgentResponse.refuse(exc.reason, trace_id=request.trace_id)

        try:
            result = await graph.ainvoke(
                {"messages": [{"role": "user", "content": self._prompt(request)}]},
                config={"recursion_limit": self._recursion_limit(request)},
            )
        except Exception as exc:
            log.exception("runtime.failed agent=%s trace=%s", self.name, request.trace_id)
            return AgentResponse.fail("agent_error", str(exc), trace_id=request.trace_id)

        return self._response(result.get("messages", []), ctx, request)

    async def stream(
        self, request: AgentRequest
    ) -> AsyncIterator[tuple[str, str] | AgentResponse]:
        """Run, yielding progress as it happens, then the final response.

        Yields ``("progress", text)`` tuples while the agent works and finishes
        with exactly one ``AgentResponse`` — so a caller can relay progress to
        a user without having to reconstruct the final answer itself. A
        multi-minute BOM sweep is unusable over a request/response API that
        says nothing until it is done; this is what the A2A layer streams.

        ``run()`` is not implemented in terms of this on purpose: the
        scheduler and in-process callers have nowhere to put progress events,
        and paying for the extra bookkeeping to discard it would be silly.
        """
        if request.budget.expired():
            yield AgentResponse.fail(
                "budget_exhausted", "deadline passed before work started",
                trace_id=request.trace_id,
            )
            return

        ctx = ToolContext(principal=request.principal, request=request)
        try:
            graph = self._build_graph(ctx)
        except Denied as exc:
            yield AgentResponse.refuse(exc.reason, trace_id=request.trace_id)
            return

        messages: list[BaseMessage] = []
        try:
            async for chunk in graph.astream(
                {"messages": [{"role": "user", "content": self._prompt(request)}]},
                config={"recursion_limit": self._recursion_limit(request)},
                stream_mode="updates",
            ):
                for update in chunk.values():
                    if not isinstance(update, dict):
                        continue
                    for message in update.get("messages", []) or []:
                        messages.append(message)
                        note = _progress_note(message)
                        if note:
                            yield ("progress", note)
        except Exception as exc:
            log.exception("runtime.failed agent=%s trace=%s", self.name, request.trace_id)
            yield AgentResponse.fail("agent_error", str(exc), trace_id=request.trace_id)
            return

        yield self._response(messages, ctx, request)


def _progress_note(message: BaseMessage) -> str:
    """A one-line, user-facing description of one step the agent just took.

    Deliberately says which tool ran rather than echoing its output: tool
    results routinely contain content the caller is not cleared to see, and a
    progress feed is not the place to leak it past the authorization the tool
    layer just applied.
    """
    if isinstance(message, AIMessage):
        names = [c["name"] for c in (message.tool_calls or [])]
        if names:
            return "Working: " + ", ".join(names)
        return ""
    if isinstance(message, ToolMessage):
        return f"Finished: {message.name}"
    return ""


def _usage_from_messages(messages: Sequence[BaseMessage]) -> Usage:
    """Sum token usage across the run.

    ``turns`` counts AI messages rather than graph steps — that is the number
    a budget in ``protocol.Budget`` is expressed in. Cost is left at zero:
    only the provider knows its own pricing, and inventing a number here would
    be worse than reporting none.
    """
    turns = input_tokens = output_tokens = 0
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        turns += 1
        meta = message.usage_metadata or {}
        input_tokens += meta.get("input_tokens", 0) or 0
        output_tokens += meta.get("output_tokens", 0) or 0
    return Usage(turns=turns, input_tokens=input_tokens, output_tokens=output_tokens)
