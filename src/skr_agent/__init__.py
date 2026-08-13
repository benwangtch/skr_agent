"""skr-agent: a deep research agent on LangChain deepagents."""

from skr_agent.assembly import Mesh, build_mesh
from skr_agent.mesh import AgentRegistry, agent_as_tool, agents_as_tools
from skr_agent.principals import service_principal, user_principal
from skr_agent.protocol import (
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
from skr_agent.runtime import DeepAgent, ToolContext

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
