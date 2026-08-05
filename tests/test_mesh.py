"""The mesh contract: agent-as-tool, principal binding, refusal handling."""

from __future__ import annotations

import json

import pytest

from skr_agent.mesh import AgentRegistry, agent_as_tool, agents_as_tools
from skr_agent.protocol import (
    AgentRequest,
    AgentResponse,
    AgentSpec,
    Budget,
    Citation,
    Denied,
    Principal,
)

ALICE = Principal(subject="alice", division="supply", roles=frozenset({"wiki.reader"}))
MALLORY = Principal(subject="mallory", division="marketing")


def spec_returning(response: AgentResponse, *, name="echo") -> AgentSpec:
    async def handler(request: AgentRequest) -> AgentResponse:
        return response

    return AgentSpec(name=name, description="test agent", handler=handler)


async def call(tool_obj, args: dict) -> str:
    """Invoke a LangChain tool and return the text the calling model sees."""
    return await tool_obj.ainvoke(args)


class TestPrincipalBinding:
    async def test_principal_comes_from_the_closure_not_the_args(self):
        seen: list[Principal] = []

        async def handler(request: AgentRequest) -> AgentResponse:
            seen.append(request.principal)
            return AgentResponse(status="ok", output="done")

        spec = AgentSpec(name="probe", description="d", handler=handler)
        t = agent_as_tool(spec, principal=ALICE, parent="copilot")

        # A model attempting to assert a different identity in the tool call.
        await call(t, {"task": "go", "principal": "mallory", "division": "finance"})

        assert len(seen) == 1
        assert seen[0].subject == "alice"
        assert seen[0].division == "supply"

    async def test_extra_args_land_in_inputs_not_identity(self):
        seen: list[AgentRequest] = []

        async def handler(request: AgentRequest) -> AgentResponse:
            seen.append(request)
            return AgentResponse(status="ok")

        t = agent_as_tool(
            AgentSpec(name="probe", description="d", handler=handler),
            principal=ALICE,
            parent="copilot",
        )
        await call(t, {"task": "go", "namespace": "finance"})
        assert seen[0].inputs == {"namespace": "finance"}
        assert seen[0].principal is ALICE

    async def test_parent_is_recorded(self):
        seen: list[AgentRequest] = []

        async def handler(request: AgentRequest) -> AgentResponse:
            seen.append(request)
            return AgentResponse(status="ok")

        t = agent_as_tool(
            AgentSpec(name="probe", description="d", handler=handler),
            principal=ALICE,
            parent="skr_agent",
        )
        await call(t, {"task": "go"})
        assert seen[0].parent_agent == "skr_agent"


class TestRendering:
    async def test_citations_are_rendered_for_the_calling_model(self):
        response = AgentResponse(
            status="ok",
            output="Acme is sole source.",
            citations=(
                Citation(kind="wiki_page", ref="supply/acme", title="Acme profile"),
                Citation(kind="raw_report", ref="rpt-1", title="W28 weekly"),
            ),
        )
        t = agent_as_tool(spec_returning(response), principal=ALICE, parent="copilot")
        text = await call(t, {"task": "who supplies ASC-4400?"})
        assert "Acme is sole source." in text
        assert "supply/acme" in text
        assert "rpt-1" in text
        assert not text.startswith("[")

    async def test_structured_data_is_rendered_as_json(self):
        response = AgentResponse(status="ok", output="ok", data={"ref": "supply/x"})
        t = agent_as_tool(spec_returning(response), principal=ALICE, parent="copilot")
        text = await call(t, {"task": "t"})
        assert '"ref": "supply/x"' in text

    async def test_refusal_is_an_error_result_with_the_reason_intact(self):
        response = AgentResponse.refuse("alice may not read namespace 'finance'")
        t = agent_as_tool(spec_returning(response), principal=ALICE, parent="copilot")
        text = await call(t, {"task": "t"})
        assert text.startswith("[refused]")
        assert "may not read namespace" in text

    async def test_denied_exception_becomes_a_refusal_not_a_crash(self):
        async def handler(request: AgentRequest) -> AgentResponse:
            raise Denied("nope")

        t = agent_as_tool(
            AgentSpec(name="probe", description="d", handler=handler),
            principal=ALICE,
            parent="copilot",
        )
        text = await call(t, {"task": "t"})
        assert text.startswith("[refused]")
        assert "nope" in text

    async def test_unexpected_exception_is_surfaced_not_swallowed(self):
        async def handler(request: AgentRequest) -> AgentResponse:
            raise RuntimeError("backend down")

        t = agent_as_tool(
            AgentSpec(name="probe", description="d", handler=handler),
            principal=ALICE,
            parent="copilot",
        )
        text = await call(t, {"task": "t"})
        assert text.startswith("[failed]")
        assert "backend down" in text


class TestCitationHarvest:
    async def test_on_response_receives_citations_across_the_hop(self):
        harvested: list[Citation] = []
        response = AgentResponse(
            status="ok",
            citations=(Citation(kind="wiki_page", ref="supply/acme"),),
        )
        t = agent_as_tool(
            spec_returning(response),
            principal=ALICE,
            parent="skr_agent",
            on_response=lambda r: harvested.extend(r.citations),
        )
        await call(t, {"task": "t"})
        assert [c.ref for c in harvested] == ["supply/acme"]


class TestRegistry:
    def test_duplicate_registration_is_rejected(self):
        registry = AgentRegistry()
        registry.register(spec_returning(AgentResponse(status="ok"), name="a"))
        with pytest.raises(ValueError):
            registry.register(spec_returning(AgentResponse(status="ok"), name="a"))

    def test_catalog_lists_name_and_description(self):
        registry = AgentRegistry()
        registry.register(spec_returning(AgentResponse(status="ok"), name="a"))
        assert "a: test agent" in registry.catalog()

    def test_agents_become_tools_named_after_themselves(self):
        tools = agents_as_tools(
            [
                spec_returning(AgentResponse(status="ok"), name="wiki_ask"),
                spec_returning(AgentResponse(status="ok"), name="skr_agent"),
            ],
            principal=ALICE,
            parent="copilot",
        )
        assert [t.name for t in tools] == ["wiki_ask", "skr_agent"]


class TestBudget:
    def test_child_budget_never_exceeds_parent(self):
        parent = Budget(max_turns=10)
        assert parent.child(max_turns=50).max_turns == 10
        assert parent.child(max_turns=3).max_turns == 3

    def test_delegate_carries_principal_and_trace(self):
        request = AgentRequest(principal=ALICE, task="parent task", budget=Budget(max_turns=10))
        child = request.delegate("child task", agent="skr_agent", max_turns=4)
        assert child.principal is ALICE
        assert child.trace_id == request.trace_id
        assert child.budget.max_turns == 4
        assert child.parent_agent == "skr_agent"


class TestPrincipalHygiene:
    def test_redacted_drops_token_and_attributes(self):
        p = Principal(
            subject="alice",
            division="supply",
            token="secret",
            attributes={"email": "alice@corp"},
        )
        r = p.redacted()
        assert r.token is None
        assert r.attributes == {}
        assert r.subject == "alice"
