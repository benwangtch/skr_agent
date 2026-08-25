"""Building the research agent, with or without a domain.

Two shapes of deployment, one agent:

**Known task, on a schedule.** The weekly supply-chain sweep is the same
investigation every week, so it is worth encoding what the units are and where
the traps are. That is a ``ResearchDomain``: sources, a briefing, specialist
subagents, a report rubric.

**Unknown task, from a user.** Someone types a question nobody anticipated.
There is no domain to pass, and the agent has to be complete without one — the
generic research loop, whatever sources the deployment mounted, and its own
judgement about how deep to go.

``domain=None`` is therefore a first-class case, not a degraded one. Everything
that makes this agent good at research lives in ``core.prompt`` and
``core.subagents`` and is present either way; a domain only ever adds.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from langchain_core.tools import BaseTool

from deep_research_agent.core.domain import ResearchDomain
from deep_research_agent.core.prompt import research_prompt
from deep_research_agent.core.reference_tools import reference_toolsets
from deep_research_agent.core.references import DEFAULT_FORMAT
from deep_research_agent.core.subagents import core_subagents
from deep_research_agent.runtime import DeepAgent, ToolContext, ToolsetFactory
from deep_research_agent.wiki.authz import WikiAuthorizer
from deep_research_agent.wiki.backend import WikiBackend
from deep_research_agent.wiki.tools import make_wiki_toolset

__all__ = ["build_research_agent", "AGENT_NAME", "DEFAULT_DESCRIPTION"]

log = logging.getLogger(__name__)

AGENT_NAME = "deep_research_agent"

DEFAULT_DESCRIPTION = (
    "Deep research on an open-ended question: plans its own investigation, "
    "works every source it has, cross-checks what it finds, and answers with "
    "the evidence attached. Call this when the answer needs research rather "
    "than a lookup."
)


def _warn_about_unloaded_skills(project_root, loaded: Sequence[str]) -> None:
    """Point out skills sitting in the repo that nothing is loading.

    The failure mode of a folder-based convention is a file that looks
    installed and silently is not: someone adds ``skills/house-style/`` for a
    scheduled job, never wires it up, and the job keeps running to the old
    rules with no signal. A warning is the cheapest way to close that gap
    without making every dropped-in folder mandatory policy.
    """
    from deep_research_agent.runtime import discover_skills

    unloaded = sorted(set(discover_skills(project_root)) - set(loaded))
    if unloaded:
        log.warning(
            "skills present but not loaded: %s. Add them to the domain's "
            "`skills`, or to SKILLS_ENABLED, to use them.",
            ", ".join(unloaded),
        )


def _env_skills(already: Sequence[str]) -> list[str]:
    """Skills named by ``SKILLS_ENABLED``, minus any already requested.

    Additive rather than replacing: a domain's rubric is what makes its output
    publishable, so an env var that could silently drop it is a footgun.
    """
    from deep_research_agent.config import get_skills

    return [n for n in get_skills().enabled_names() if n not in already]


def _input_schema(domain: ResearchDomain | None, publishable: bool) -> dict[str, Any]:
    """The agent's structured-input contract, which is also its A2A card.

    ``task`` is always free text — that is the face-to-user path, and a
    deployment with no domain accepts anything. A domain merges in its own
    properties so a scheduled caller can pass parameters instead of prose.
    """
    properties: dict[str, Any] = {
        "task": {
            "type": "string",
            "description": "The question to research, in full sentences.",
        }
    }
    if publishable:
        properties["publish"] = {
            "type": "boolean",
            "description": "Whether to publish the finished report. Default true.",
        }
    if domain is not None:
        properties.update(domain.inputs)
    return {"type": "object", "properties": properties, "required": ["task"]}


def build_research_agent(
    *,
    wiki_backend: WikiBackend,
    wiki_authz: WikiAuthorizer,
    project_root: str | Path,
    domain: ResearchDomain | None = None,
    model: str | None = None,
    max_turns: int = 60,
    publishable: bool = True,
    check_references: bool = True,
    extra_toolsets: Sequence[ToolsetFactory] = (),
) -> DeepAgent:
    """Wire up the research agent.

    ``domain`` is optional; ``None`` gives the general agent, which is what a
    user typing an arbitrary question gets.

    The wiki is mounted either way. It is not a domain's data source — it is
    the internal-knowledge source every research task wants and, when
    ``publishable``, the place finished work lands. It is also the only source
    carrying an authorization model, which is enforced in its own tool layer
    against the triggering principal, so this module never learns the rules.

    ``extra_toolsets`` adds sources beyond the domain's — MCP servers arrive
    this way (``deep_research_agent.mcp``). They are loaded once at startup by
    the caller, because discovery is async and toolset factories are not.

    ``publishable=False`` drops the write tool *and* the prompt section that
    describes it, so a read-only deployment does not spend turns attempting a
    call it cannot make.

    ``check_references=False`` does the same for the reference checker. Leave
    it on unless the output genuinely has no citation convention — a checker
    with the wrong format reports every section as unsourced, which is how a
    useful check gets ignored. Prefer overriding the domain's
    ``reference_format`` to turning it off.
    """
    # A domain may define its own citation shape; None here means "off",
    # which is why the flag and the domain's override are resolved together.
    reference_format = None
    if check_references:
        reference_format = (domain.reference_format if domain else None) or DEFAULT_FORMAT

    skills = list(domain.skills) if domain else []
    skills += _env_skills(skills)
    if domain is not None:
        # Only meaningful for a configured deployment. With no domain every
        # rubric in the repo is legitimately unused, and warning about all of
        # them on every run is how a useful warning gets tuned out.
        _warn_about_unloaded_skills(project_root, skills)

    def subagents(ctx: ToolContext, by_name: dict[str, BaseTool]) -> list[dict[str, Any]]:
        # Selection is by declared capability (core.capabilities), not by a
        # list of tool names: with domains and MCP servers the name list can
        # never be complete, and being incomplete means a subagent silently
        # holding a tool that mutates.
        return core_subagents(
            list(by_name.values()), by_name, domain.specialists if domain else ()
        )

    return DeepAgent(
        name=AGENT_NAME,
        description=(domain.summary or DEFAULT_DESCRIPTION) if domain else DEFAULT_DESCRIPTION,
        system_prompt=research_prompt(
            briefing=domain.briefing if domain else "",
            publishable=publishable,
            check_references=check_references,
        ),
        toolsets=[
            make_wiki_toolset(
                wiki_backend, wiki_authz,
                writable=publishable,
                # Only when the checker is actually mounted -- a gate nothing
                # can open is worse than no gate.
                require_reference_check=reference_format is not None,
            ),
            *reference_toolsets(
                reference_format, domain.reference_rules if domain else ()
            ),
            *(domain.toolsets if domain else ()),
            *extra_toolsets,
        ],
        subagents=subagents,
        skills=skills,
        project_root=project_root,
        model=model,
        max_turns=max_turns,
        input_schema=_input_schema(domain, publishable),
    )
