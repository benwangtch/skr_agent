"""Sending a run's trace to Langfuse.

What you get in Langfuse, and where each part comes from:

* **Every tool call** — LangChain emits a callback per tool run, so each one
  becomes a span with its arguments, its result, and its duration. Nothing in
  this repo has to enumerate the tools.
* **Every MCP call, identified as such** — a tool span alone cannot tell you
  whether ``get_supplier_risk`` came from an MCP server or from this codebase.
  ``mcp.py`` therefore stamps ``tool_source="mcp"`` and the server name onto
  each wrapped tool's metadata, and it rides along to the span.
* **Every subagent** — the ``task`` tool's input names the ``subagent_type``,
  and the subagent's own graph runs as a nested chain span named after it. So
  a delegation shows up twice, deliberately: once as the decision to delegate,
  once as the work.
* **Who asked** — the trace carries the ``Principal``'s subject as the
  Langfuse user, plus division and roles. Two runs of the same question under
  different principals are the same shape with different scope, and that is
  usually what you are trying to see.

Two rules this module holds to:

**Tracing must never break a run.** Every failure path here degrades to "no
tracing" and logs once. An observability backend being down is not a reason
for a research job to fail.

**Nothing is sent unless configured.** No keys, no handler, no network.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from deep_research_agent.config import get_langfuse

if TYPE_CHECKING:
    from deep_research_agent.protocol import AgentRequest

log = logging.getLogger(__name__)

__all__ = ["langfuse_handler", "trace_metadata", "run_config", "flush"]

_UNAVAILABLE_LOGGED = False


def langfuse_handler():
    """The LangChain callback handler, or ``None`` when tracing is off.

    Configuring the SDK client is global and idempotent, so this is safe to
    call per request; the handler itself is cheap.
    """
    global _UNAVAILABLE_LOGGED

    config = get_langfuse()
    for problem in config.problems():
        log.warning("langfuse.misconfigured %s -- tracing is off", problem)
    if not config.configured():
        return None

    try:
        from langfuse import Langfuse as LangfuseClient
        from langfuse.langchain import CallbackHandler
    except ImportError:
        if not _UNAVAILABLE_LOGGED:
            log.warning("langfuse configured but the package is not installed")
            _UNAVAILABLE_LOGGED = True
        return None

    try:
        LangfuseClient(
            public_key=config.public_key,
            secret_key=config.secret_key.get_secret_value(),
            host=config.base_url,
            **({"environment": config.environment} if config.environment else {}),
        )
        return CallbackHandler()
    except Exception:
        # Deliberately broad: an unreachable host, a rejected key, an OTel
        # exporter that will not start -- none of them justify failing the run
        # the caller actually asked for.
        if not _UNAVAILABLE_LOGGED:
            log.exception("langfuse.setup_failed -- continuing without tracing")
            _UNAVAILABLE_LOGGED = True
        return None


def trace_metadata(agent_name: str, request: "AgentRequest") -> dict[str, Any]:
    """Trace-level attributes for one run.

    ``langfuse_*`` keys are the handler's documented channel for naming a
    trace and attaching a user, session and tags; everything else lands as
    ordinary trace metadata.

    ``trace_id`` is used as the session id so that a delegation chain, an A2A
    task and the run it triggered group together — A2A already threads its
    ``task_id`` through as the ``trace_id``.
    """
    principal = request.principal
    return {
        "langfuse_trace_name": agent_name,
        "langfuse_user_id": principal.subject,
        "langfuse_session_id": request.trace_id,
        "langfuse_tags": [
            f"agent:{agent_name}",
            f"division:{principal.division}",
            f"actor:{principal.attributes.get('actor_type', 'unknown')}",
        ],
        "trace_id": request.trace_id,
        "principal_subject": principal.subject,
        "principal_division": principal.division,
        "principal_roles": sorted(principal.roles),
        "parent_agent": request.parent_agent or "",
        "budget_max_turns": request.budget.max_turns,
        "inputs": request.inputs,
    }


def run_config(agent_name: str, request: "AgentRequest", **extra: Any) -> dict[str, Any]:
    """The ``config=`` dict for a graph invocation.

    Returns callbacks and metadata together because they are only useful
    together: the handler is what exports, the metadata is what makes the
    exported trace searchable by who ran it.
    """
    config: dict[str, Any] = dict(extra)
    handler = langfuse_handler()
    if handler is not None:
        config["callbacks"] = [handler]
        config["metadata"] = trace_metadata(agent_name, request)
    return config


def flush() -> None:
    """Push buffered traces. Call before a short-lived process exits.

    The SDK batches in the background, so a script that finishes immediately
    after a run can exit with its trace still in the buffer.
    """
    if not get_langfuse().configured():
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        log.debug("langfuse.flush_failed", exc_info=True)
