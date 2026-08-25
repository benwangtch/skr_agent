"""What a tool is allowed to do, declared by the tool rather than guessed.

Two safety properties hold this agent together, and both have to survive an
unknown tool surface — the point of the agent is that a deployment can mount
any set of sources, and a face-to-user deployment does not know the question
in advance, let alone the tools:

1. **Only the top-level agent changes anything.** No subagent publishes; a
   report reaches the wiki only after the fact-checker has passed it.
2. **The fact-checker cannot go looking for new material.** A checker that can
   search starts researching instead of checking, and "confirms" a claim from
   a source the report never cited.

The old implementation encoded both as hand-written lists of tool *names* in
the agent module. That works for exactly one domain with one fixed set of
tools. Add a domain, or an MCP server, and the list is silently incomplete —
and the failure mode is a subagent quietly gaining a tool that mutates.

So capability moves onto the tool, along two axes:

    mutates    changes state something else can later observe
    discovers  takes a query and returns things you did not already know of

which gives three declarations a toolset uses at construction:

    lookup(tool)     read-only, fetches a named thing      -> anyone, including the checker
    search(tool)     read-only, finds things by query      -> researchers, not the checker
    mutating(tool)   changes state                         -> top-level agent only

**Unknown means unsafe.** An undeclared tool is treated as mutating. A tool
arriving from somewhere this repo does not control — an MCP server, most
importantly — cannot be assumed harmless because its name reads like a
lookup. Wrong in one direction costs a subagent a tool; wrong in the other is
an undeclared mutation with no fact-checker in front of it.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping, TypeVar

from langchain_core.tools import BaseTool

log = logging.getLogger(__name__)

__all__ = [
    "lookup",
    "search",
    "mutating",
    "is_read_only",
    "is_lookup",
    "read_only_tools",
    "lookup_tools",
    "select_read_only",
]

_MUTATES = "mutates"
_DISCOVERS = "discovers"

T = TypeVar("T", bound=BaseTool)


def _declare(tool: T, *, mutates: bool, discovers: bool) -> T:
    tool.metadata = {**(tool.metadata or {}), _MUTATES: mutates, _DISCOVERS: discovers}
    return tool


def lookup(tool: T) -> T:
    """Read-only, retrieves one named thing: a page by ref, an article by URL.

    The narrowest capability, and the only one the fact-checker gets.
    """
    return _declare(tool, mutates=False, discovers=False)


def search(tool: T) -> T:
    """Read-only, but finds material the caller did not already know about.

    Safe for any researching subagent; deliberately withheld from the checker.
    """
    return _declare(tool, mutates=False, discovers=True)


def mutating(tool: T) -> T:
    """Changes state somewhere outside this process — a published page, a
    ticket, a message, a row. Top-level agent only."""
    return _declare(tool, mutates=True, discovers=False)


def is_read_only(tool: BaseTool) -> bool:
    """Fail closed — an undeclared tool is treated as if it mutates."""
    return (tool.metadata or {}).get(_MUTATES) is False


def is_lookup(tool: BaseTool) -> bool:
    """Read-only *and* non-discovering. Fails closed on both axes."""
    return is_read_only(tool) and (tool.metadata or {}).get(_DISCOVERS) is False


def read_only_tools(tools: Iterable[BaseTool]) -> list[BaseTool]:
    """Everything a researching subagent may be given, in build order."""
    return [t for t in tools if is_read_only(t)]


def lookup_tools(tools: Iterable[BaseTool]) -> list[BaseTool]:
    """Everything the fact-checker may be given."""
    return [t for t in tools if is_lookup(t)]


def select_read_only(
    names: Iterable[str], by_name: Mapping[str, BaseTool], *, requested_by: str
) -> list[BaseTool]:
    """The named tools, minus any that are not read-only.

    A domain names the tools its specialist needs. It cannot widen the safety
    rule by doing so: a name resolving to a mutating tool is dropped and
    logged rather than trusted because a domain asked for it. Missing names
    are skipped quietly — a domain may legitimately name a tool this
    deployment did not mount.
    """
    selected: list[BaseTool] = []
    for name in names:
        tool = by_name.get(name)
        if tool is None:
            continue
        if not is_read_only(tool):
            log.warning(
                "subagent %s asked for tool %r, which is not declared read-only "
                "-- dropping it. Only the top-level agent may use it.",
                requested_by, name,
            )
            continue
        selected.append(tool)
    return selected
