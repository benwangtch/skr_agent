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

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from skr_agent.config.base import BaseConfig

log = logging.getLogger(__name__)

__all__ = ["MCP", "get_mcp"]

Transport = Literal["streamable_http", "sse", "stdio", "websocket"]


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
    the note on identity in ``skr_agent.mcp``."""

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
