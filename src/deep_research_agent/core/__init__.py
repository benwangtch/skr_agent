"""The subject-independent research agent: prompt, subagents, capabilities.

Nothing in this package mentions a supplier, a company or a bill of materials.
What lives here is what makes the agent good at research regardless of what it
is researching — the method, the evidence rules, the scratchpad, the checker —
plus the capability model that keeps those guarantees true on a tool surface
nobody enumerated in advance.

Subject-matter knowledge is a ``ResearchDomain`` from
``deep_research_agent.domains``, and it is optional.
"""

from deep_research_agent.core.agent import AGENT_NAME, build_research_agent
from deep_research_agent.capabilities import (
    is_lookup,
    is_read_only,
    lookup,
    mutating,
    search,
)
from deep_research_agent.core.domain import ResearchDomain, Specialist
from deep_research_agent.core.prompt import FINDINGS_DIR, research_prompt

__all__ = [
    "AGENT_NAME",
    "build_research_agent",
    "ResearchDomain",
    "Specialist",
    "research_prompt",
    "FINDINGS_DIR",
    "lookup",
    "search",
    "mutating",
    "is_read_only",
    "is_lookup",
]
