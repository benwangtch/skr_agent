"""skr-agent: a deep research agent on LangChain deepagents."""

from .assembly import Mesh, build_mesh
from .mesh import AgentRegistry, agent_as_tool, agents_as_tools
from .principals import service_principal, user_principal
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
    "AgentRegistry",
    "agent_as_tool",
    "agents_as_tools",
    "service_principal",
    "user_principal",
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
