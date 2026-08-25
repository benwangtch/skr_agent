"""A research domain: what a known subject area adds to the general agent.

The agent works with no domain at all. That is the face-to-user case — someone
types a question nobody anticipated, and the agent has the generic research
loop, whatever sources the deployment mounted, and its own judgement. Nothing
here is required for that to work.

A ``ResearchDomain`` is what you add when the task *is* known in advance,
which is what a scheduled job is: the weekly supply-chain sweep runs the same
shape of investigation every week, so encoding "here is what a BOM company is,
here is how to work an alias, here is when to call it critical" is worth doing
once instead of hoping the model rederives it each run.

So a domain is additive, never a replacement:

    briefing    a prompt section describing the sources and how to work them
    toolsets    the domain's own data sources
    specialists subagents that know the domain's shape of sub-task
    skills      rubrics the output must follow
    inputs      extra structured-input properties on the agent's schema

A domain cannot loosen anything. Its specialists' tools are filtered through
``capabilities.select_read_only``, so a domain that names a mutating tool for
a subagent gets it dropped and logged rather than honoured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from deep_research_agent.core.references import ReferenceFormat, ReferenceRule
from deep_research_agent.runtime import ToolsetFactory

__all__ = ["ResearchDomain", "Specialist"]


@dataclass(frozen=True)
class Specialist:
    """A subagent that knows one domain-specific shape of sub-task.

    ``tools`` names tools by the name the toolset gave them. Naming rather
    than passing objects is deliberate: the domain is defined once at import
    time, while tools are rebuilt per request bound to that request's
    principal, so there is no tool object to hold onto.
    """

    name: str
    description: str
    """Shown to the lead agent. This is what it decides to delegate on, so
    write it as "when to use me", not as a title."""

    system_prompt: str
    tools: Sequence[str] = ()


@dataclass(frozen=True)
class ResearchDomain:
    """Subject-matter knowledge layered onto the general research agent."""

    name: str
    """Short identifier, e.g. ``supply-chain``. Shows up in the agent card."""

    summary: str = ""
    """One line for the agent's description — what this deployment researches.
    Callers and agent registries read this to decide whether to route here."""

    briefing: str = ""
    """A prompt section: what the sources are, what the domain's units are
    (a company, a part number, a jurisdiction), and the domain-specific traps.
    Inserted after the generic role and before the generic method."""

    toolsets: Sequence[ToolsetFactory] = ()
    specialists: Sequence[Specialist] = ()
    skills: Sequence[str] = ()

    inputs: dict[str, Any] = field(default_factory=dict)
    """Extra JSON-schema properties merged into the agent's input schema, so a
    scheduled caller can pass structured parameters (``tier``, ``region``)
    rather than encoding them in prose."""

    reference_format: ReferenceFormat | None = None
    """How this domain's output cites its sources, for ``check_references``.

    ``None`` means "use the core default", which matches the shape the
    ``incident-report`` rubric asks for. A domain whose deliverable is laid
    out differently — a patent brief, a filing summary — supplies its own
    rather than editing the core one, since the checker is otherwise going to
    report every section of its output as unsourced.

    ``build_research_agent(check_references=False)`` is the escape hatch for a
    deployment with no citation convention at all."""

    reference_rules: Sequence[ReferenceRule] = ()
    """Extra reference checks this domain adds, merged into the single
    ``check_references`` call rather than shipped as a second checker.

    Two checkers would mean two verdicts and two gates, and a draft failing
    only the domain's would still be publishable through the other. One check,
    one approval, one thing to pass."""
