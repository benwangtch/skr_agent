"""The agent's entry points.

These were in an ``examples/`` folder, which undersold them: ``report`` is
what the scheduled job runs, ``ask`` is the face-to-user path, ``serve`` is
the A2A server, and ``check`` is a deployment gate that exits non-zero. None
of them is a demonstration of anything — they are how you run this.

Being inside the package is also what makes ``python -m deep_research_agent``
work at all, and what lets a container run the agent without a checkout.

Each module stands alone (``python -m deep_research_agent.cli.ask``) and is
also reachable through the dispatcher in ``__main__.py``. Every one exposes
``main(argv=None) -> int`` so neither route is the privileged one.
"""

__all__: list[str] = []
