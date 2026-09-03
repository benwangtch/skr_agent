"""Which MCP servers the agent may call tools on.

Nothing here is on by default: with no ``MCP_*`` variables set, ``connections()``
returns ``{}``, the loader returns no tools, and the agent's surface is exactly
what it was before. That matters because an MCP server is a live dependency —
if one is unreachable the agent's tool list changes shape, so it should only
appear when someone deliberately configured it.

Two ways to configure, because the common case and the general case want
different shapes:

* **One server** (typical): ``MCP_URL`` plus optionally ``MCP_TOKEN``.
* **Several** : ``MCP_SERVERS`` as a JSON object, which is passed through to
  ``MultiServerMCPClient`` almost verbatim. Use this for stdio servers too —
  a local subprocess has no URL to put in ``MCP_URL``.

``MCP_SERVERS`` wins when both are set, rather than merging: a half-merged
connection map is far harder to debug than an obviously-ignored variable.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

from deep_research_agent.config.base import BaseConfig

log = logging.getLogger(__name__)

__all__ = ["MCP", "get_mcp"]

Transport = Literal["streamable_http", "sse", "stdio", "websocket"]

_CAPABILITY_LEVELS = frozenset({"lookup", "search", "mutating"})


class MCP(BaseConfig):
    model_config = SettingsConfigDict(env_prefix = 'mcp_')

    url                 : str | None        = None
    """Single-server convenience. An MCP endpoint, e.g.
    ``https://mcp.internal.corp/mcp``. Ignored when ``servers`` is set."""

    transport           : Transport         = 'streamable_http'
    """Transport for ``url``. ``streamable_http`` is what most hosted MCP
    services speak; ``sse`` is the older HTTP transport."""

    token               : SecretStr         = SecretStr('')
    """Sent as ``Authorization: Bearer <token>`` on every call to ``url``.

    This is a *connection-level* service credential, not the end user's — see
    the note on identity in ``deep_research_agent.mcp``."""

    server_name         : str               = 'mcp'
    """Label for the ``url`` server. Only shows up in logs and errors."""

    servers             : dict[str, dict[str, Any]] = {}
    """Full connection map, as JSON, for more than one server or for stdio.
    Overrides ``url``/``transport``/``token`` entirely when set.

    Example::

        MCP_SERVERS={"risk":{"transport":"streamable_http","url":"https://..."},
                     "local":{"transport":"stdio","command":"python","args":["s.py"]}}
    """

    timeout             : int               = 60  # secs

    capabilities        : dict[str, str]     = {}
    """What each MCP tool is allowed to do, so subagents can be given the safe
    ones. Maps a tool name to ``lookup``, ``search`` or ``mutating``.

    Tools arriving from an MCP server carry no capability declaration, and
    ``capabilities.py`` treats undeclared as mutating — correctly, since
    nothing in the protocol says whether a tool writes. The consequence is that
    an MCP tool reaches the top-level agent only: no researcher subagent gets
    it, and neither does the reference checker. For a genuinely read-only tool
    that is a real loss, and this is where an operator says so.

    Saying so is a claim about someone else's service, which is why it is
    configuration rather than a guess from the tool's name: ``search_*`` is a
    naming convention, not a guarantee, and a wrong guess here puts an
    undeclared mutation behind the fact-checker.

    Key by bare tool name, or ``server/tool`` when two servers export the same
    name — the qualified form wins. Example::

        MCP_CAPABILITIES={"search_wiki_pages":"search","get_page":"lookup"}

    Anything unlisted stays mutating. See ``deep_research_agent.capabilities``
    for what each level may do."""

    wiki_page_tools     : list[str]          = []
    """MCP tools whose results are wiki pages, and should be recorded in the
    run's corpus as such.

    Opt-in by name, because reading a result means knowing its shape. A tool
    listed here is expected to return (or return a JSON object containing) a
    ``hits`` list whose entries carry ``namespace`` and ``page_name``, plus
    optionally ``description`` and ``content`` — the shape
    ``search_wiki_pages`` returns. Each hit becomes a ``RetrievedDocument``
    keyed by ``page_ref(namespace, page_name)``, which is what lets the
    reference formatter build a real wiki link for a page the agent found
    through MCP rather than through this repo's own wiki tools.

    Naming the tools rather than sniffing every result is deliberate: this is
    a coupling to another service's JSON, and it should be visible in
    configuration. When that service renames ``hits``, a listed tool that
    parses to nothing logs a warning naming itself — the alternative is
    citations quietly going missing from reports.

    Anything unlisted is untouched: its result still reaches the model and
    still records the ``mcp://`` citation, it just does not enter the corpus.

        MCP_WIKI_PAGE_TOOLS=["search_wiki_pages"]
    """

    def capability_of(self, server: str, tool: str) -> str | None:
        """The declared capability for one tool, or ``None`` if undeclared.

        ``None`` means the fail-closed default applies. Qualified beats bare so
        that two servers exporting ``search`` can be declared separately.
        """
        return self.capabilities.get(f"{server}/{tool}") or self.capabilities.get(tool)

    @field_validator("capabilities")
    @classmethod
    def _known_capabilities(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject a misspelt level rather than silently leaving it undeclared.

        A typo would otherwise read as "not declared", which fails closed and
        looks identical to forgetting the entry — the operator sees a tool
        missing from a subagent and no reason why.
        """
        unknown = {k: v for k, v in value.items() if v not in _CAPABILITY_LEVELS}
        if unknown:
            raise ValueError(
                f"unknown capability level(s) in MCP_CAPABILITIES: {unknown}. "
                f"Use one of {sorted(_CAPABILITY_LEVELS)}."
            )
        return value

    def connections(self) -> dict[str, dict[str, Any]]:
        """The connection map for ``MultiServerMCPClient``, or ``{}``.

        Empty means "no MCP configured", which every caller treats as "add no
        tools" rather than as an error.
        """
        if self.servers:
            return self.servers
        if not self.url:
            return {}

        connection: dict[str, Any] = {
            "transport": self.transport,
            "url": self.url,
            "timeout": self.timeout,
        }
        secret = self.token.get_secret_value()
        if secret:
            connection["headers"] = {"Authorization": f"Bearer {secret}"}
        return {self.server_name: connection}

    def configured(self) -> bool:
        return bool(self.connections())


@lru_cache(maxsize=1)
def get_mcp() -> MCP:
    return MCP()
