"""skr-agent: a small agent mesh — copilot, wiki coordinator, report agent."""

from .assembly import Mesh, build_mesh
from .copilot import build_copilot
from .mesh import AgentRegistry, agent_as_tool, agents_as_toolserver
from .protocol import (
    AgentError,
    AgentRequest,
    AgentResponse,
    AgentSpec,
    Budget,
    Citation,
    Denied,
    Principal,
    Usage,
)
from .runtime import DeepAgent, ToolContext

__all__ = [
    "Mesh",
    "build_mesh",
    "build_copilot",
    "AgentRegistry",
    "agent_as_tool",
    "agents_as_toolserver",
    "AgentRequest",
    "AgentResponse",
    "AgentSpec",
    "AgentError",
    "Budget",
    "Citation",
    "Denied",
    "Principal",
    "Usage",
    "DeepAgent",
    "ToolContext",
]
