"""The mesh contract: agent-as-tool, principal binding, refusal handling."""

from __future__ import annotations

import json

import pytest

from skr_agent.mesh import AgentRegistry, agent_as_tool, agents_as_toolserver
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


async def call(tool_obj, args: dict) -> dict:
    """Invoke the underlying coroutine of an SDK tool object."""
    handler = getattr(tool_obj, "handler", None) or tool_obj
    return await handler(args)


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
            parent="wiki_report",
        )
        await call(t, {"task": "go"})
        assert seen[0].parent_agent == "wiki_report"


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
        out = await call(t, {"task": "who supplies ASC-4400?"})
        text = out["content"][0]["text"]
        assert "Acme is sole source." in text
        assert "supply/acme" in text
        assert "rpt-1" in text
        assert not out.get("is_error")

    async def test_structured_data_is_rendered_as_json(self):
        response = AgentResponse(status="ok", output="ok", data={"ref": "supply/x"})
        t = agent_as_tool(spec_returning(response), principal=ALICE, parent="copilot")
        text = (await call(t, {"task": "t"}))["content"][0]["text"]
        assert '"ref": "supply/x"' in text

    async def test_refusal_is_an_error_result_with_the_reason_intact(self):
        response = AgentResponse.refuse("alice may not read namespace 'finance'")
        t = agent_as_tool(spec_returning(response), principal=ALICE, parent="copilot")
        out = await call(t, {"task": "t"})
        assert out["is_error"] is True
        assert "may not read namespace" in out["content"][0]["text"]

    async def test_denied_exception_becomes_a_refusal_not_a_crash(self):
        async def handler(request: AgentRequest) -> AgentResponse:
            raise Denied("nope")

        t = agent_as_tool(
            AgentSpec(name="probe", description="d", handler=handler),
            principal=ALICE,
            parent="copilot",
        )
        out = await call(t, {"task": "t"})
        assert out["is_error"] is True
        assert "nope" in out["content"][0]["text"]

    async def test_unexpected_exception_is_surfaced_not_swallowed(self):
        async def handler(request: AgentRequest) -> AgentResponse:
            raise RuntimeError("backend down")

        t = agent_as_tool(
            AgentSpec(name="probe", description="d", handler=handler),
            principal=ALICE,
            parent="copilot",
        )
        out = await call(t, {"task": "t"})
        assert out["is_error"] is True
        assert "backend down" in out["content"][0]["text"]


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
            parent="wiki_report",
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

    def test_toolserver_names_are_mcp_qualified(self):
        _, names = agents_as_toolserver(
            [spec_returning(AgentResponse(status="ok"), name="wiki_ask")],
            principal=ALICE,
            parent="copilot",
            server_name="wiki",
        )
        assert names == ["mcp__wiki__wiki_ask"]


class TestBudget:
    def test_child_budget_never_exceeds_parent(self):
        parent = Budget(max_turns=10)
        assert parent.child(max_turns=50).max_turns == 10
        assert parent.child(max_turns=3).max_turns == 3

    def test_delegate_carries_principal_and_trace(self):
        request = AgentRequest(principal=ALICE, task="parent task", budget=Budget(max_turns=10))
        child = request.delegate("child task", agent="wiki_report", max_turns=4)
        assert child.principal is ALICE
        assert child.trace_id == request.trace_id
        assert child.budget.max_turns == 4
        assert child.parent_agent == "wiki_report"


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
