"""Subject-matter packs for the research agent.

A domain is what you add when the task is known in advance — which, in
practice, is what a scheduled job is. It supplies sources, a briefing, any
specialist subagents, and a report rubric. It cannot loosen a safety rule:
specialists' tools are filtered through the read-only check in
``deep_research_agent.capabilities`` regardless of what a domain asks for.

Adding one is a sibling package to ``supply_chain/``, not an edit to
``core/``. If you find yourself changing ``core/`` to add a domain, the thing
you are adding is probably general and belongs there on its own merits.

No domain is required. ``build_research_agent(domain=None)`` is the
face-to-user agent: an arbitrary question, the generic research loop, and
whatever sources the deployment mounted.
"""

__all__: list[str] = []
