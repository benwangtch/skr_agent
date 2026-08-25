"""Turning a startup misconfiguration into one actionable line.

Without this, the most common setup mistake — no ``LLM_API_KEY`` — surfaces as
a thirty-line traceback from inside the provider SDK, ending in *"set the
OPENAI_API_KEY environment variable"*. That advice is wrong here: the variable
is ``LLM_API_KEY``, and it may be authenticating against an internal gateway
that has never heard of OpenAI. Someone following it sets the wrong variable
and gets the same traceback.

The check is a precondition on the config, not a translation of the
exception. Pattern-matching provider error text would break the first time a
provider reworded it, and would say nothing about a *different* misconfigured
provider.
"""

from __future__ import annotations

import sys

from deep_research_agent.config import get_llm

__all__ = ["require_llm", "warn_about_llm"]

_ADVICE = "Run `python -m deep_research_agent check` for the full picture."


def require_llm() -> None:
    """Stop before doing any work if the model could not possibly be reached.

    Raises ``SystemExit(2)`` — distinct from 1, which the commands use for "it
    ran and the result was a failure". A caller scripting these can tell "I
    configured it wrong" from "the research did not succeed".
    """
    problems = get_llm().problems()
    if not problems:
        return
    print("Cannot start:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(f"\n{_ADVICE}", file=sys.stderr)
    raise SystemExit(2)


def warn_about_llm() -> None:
    """Same check, but only a warning — for the server.

    ``serve`` deliberately starts without credentials: the A2A app and the
    scheduler build no model client until a task actually arrives, and a
    deployment may inject the key after the container starts. Refusing to boot
    would be wrong. Booting *silently* into a server that will fail every
    request is also wrong, so it says so once.
    """
    for problem in get_llm().problems():
        print(f"warning: {problem}", file=sys.stderr)
