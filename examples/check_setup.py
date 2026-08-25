#!/usr/bin/env python3
"""Check the setup before spending tokens on a real run.

    uv run python examples/check_setup.py            # config + MCP connectivity
    uv run python examples/check_setup.py --llm      # also send one tiny real request

Answers "is this configured correctly" without running the agent. Everything
except --llm is free: reading config costs nothing, and connecting to an MCP
server only lists its tools.

Exits non-zero if something is wrong, so it works as a deployment gate.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK, BAD, WARN, INFO = "  [ok]", "  [!!]", "  [--]", "     "


def check_llm(probe: bool) -> list[str]:
    from deep_research_agent.config import get_llm

    problems: list[str] = []
    print("LLM")
    llm = get_llm()
    print(f"{INFO} provider : {llm.provider}")
    print(f"{INFO} base_url : {llm.resolved_base_url() or '(provider default)'}")
    print(f"{INFO} model    : {llm.resolved_model() or '(none)'}")

    if not llm.api_key.get_secret_value():
        print(f"{BAD} LLM_API_KEY is empty -- the agent will fail at startup.")
        problems.append("LLM_API_KEY not set")
    else:
        print(f"{OK} LLM_API_KEY is set")

    if llm.provider == "custom" and not llm.model:
        print(f"{BAD} LLM_PROVIDER=custom needs LLM_MODEL (no default for an unknown host).")
        problems.append("LLM_MODEL not set for provider=custom")

    if not probe:
        print(f"{WARN} not contacting the model (pass --llm to actually try it)")
        return problems

    try:
        model = llm.build_chat_model()
        reply = model.invoke("Reply with the single word: ok")
        text = getattr(reply, "content", "")
        print(f"{OK} model answered: {str(text)[:60]!r}")
    except Exception as exc:  # the whole point is to surface this early
        print(f"{BAD} model call failed: {type(exc).__name__}: {exc}")
        problems.append("LLM request failed")
    return problems


async def check_mcp(verbose: bool) -> list[str]:
    import logging

    from deep_research_agent.config import get_mcp
    from deep_research_agent.mcp import load_mcp_tools

    # load_mcp_tools logs the full traceback on failure, which production wants
    # and a human running a setup check does not: 40 lines of anyio/httpx frames
    # bury the one line that says which server is unreachable. Quiet it here and
    # print our own summary; -v puts the detail back.
    mcp_log = logging.getLogger("deep_research_agent.mcp")
    if not verbose:
        mcp_log.setLevel(logging.CRITICAL)

    problems: list[str] = []
    print("\nMCP")
    config = get_mcp()
    connections = config.connections()
    if not connections:
        print(f"{WARN} no MCP server configured (set MCP_URL, or MCP_SERVERS)")
        print(f"{INFO} this is fine -- the agent runs on its built-in sources")
        return problems

    for name, connection in connections.items():
        target = connection.get("url") or connection.get("command", "?")
        auth = "with token" if connection.get("headers") else "no token"
        print(f"{INFO} {name}: {connection.get('transport')} -> {target} ({auth})")

    loaded = await load_mcp_tools(config)
    by_server: dict[str, list[str]] = {}
    for server, tool in loaded:
        by_server.setdefault(server, []).append(tool.name)

    for name in connections:
        tools = by_server.get(name)
        if not tools:
            print(f"{BAD} {name}: could not connect, or it exposed no tools")
            print(f"{INFO} re-run with -v to see why (URL wrong? token rejected? "
                  f"host unreachable?)")
            problems.append(f"MCP server {name!r} unusable")
        else:
            print(f"{OK} {name}: {len(tools)} tool(s) -- {', '.join(tools)}")
    return problems


def check_skills() -> list[str]:
    from deep_research_agent.core.agent import _env_skills
    from deep_research_agent.domains import supply_chain
    from deep_research_agent.runtime import discover_skills, load_skill, skill_roots

    problems: list[str] = []
    print("\nSkills")
    print(f"{INFO} search path: {', '.join(str(r) for r in skill_roots(ROOT))}")

    available = discover_skills(ROOT)
    # Checked against the scheduled deployment, which is the one whose skills
    # have to resolve unattended. A face-to-user run with no domain loads only
    # what SKILLS_ENABLED names.
    domain_skills = list(supply_chain.from_fixtures(ROOT / "fixtures").skills)
    loaded = domain_skills + _env_skills(domain_skills)

    for name in loaded:
        try:
            body = load_skill(ROOT, name)
        except FileNotFoundError as exc:
            print(f"{BAD} {name}: {exc}".replace("\n", " "))
            problems.append(f"skill {name!r} not found")
        else:
            print(f"{OK} {name}: loaded ({len(body)} chars, inlined into the prompt)")

    for name in sorted(set(available) - set(loaded)):
        print(f"{WARN} {name}: present but NOT loaded "
              f"(add it to a domain's `skills`, or to SKILLS_ENABLED)")
    return problems


def check_langfuse() -> list[str]:
    from deep_research_agent.config import get_langfuse
    from deep_research_agent.observability import langfuse_handler

    problems: list[str] = []
    print("\nLangfuse (tracing)")
    config = get_langfuse()

    for problem in config.problems():
        print(f"{BAD} {problem}")
        problems.append(problem)

    if not config.configured():
        print(f"{WARN} tracing is off (set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY)")
        print(f"{INFO} the agent runs normally without it")
        return problems

    print(f"{INFO} host   : {config.base_url}")
    print(f"{INFO} env    : {config.environment or '(langfuse default)'}")
    if langfuse_handler() is None:
        print(f"{BAD} configured, but no handler could be built -- see the log above")
        problems.append("Langfuse handler unavailable")
    else:
        print(f"{OK} handler ready -- runs will export tool calls, MCP calls and subagents")
        print(f"{INFO} reachability is not checked here; the SDK exports in the "
              f"background, so a wrong host shows up as missing traces, not an error")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--llm", action="store_true",
        help="Send one tiny real request to the model. Costs a negligible "
        "amount and is the only way to prove the endpoint and key actually work.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        import logging

        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    problems = check_llm(args.llm)
    problems += asyncio.run(check_mcp(args.verbose))
    problems += check_skills()
    problems += check_langfuse()

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("Setup looks good."
          + ("" if args.llm else " (re-run with --llm to prove the model endpoint works.)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
