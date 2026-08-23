"""The deep research agent this repo builds.

It answers open-ended supply-chain questions by working across every source it
has — the bill of materials, external news, and the internal wiki — and
publishing what it finds. Each of those is one data source; none of them is
the point. The point is the research loop: how many aliases to search per
company, whether a hit is worth reading in full, whether an external incident
warrants cross-referencing internal history, when the picture is complete
enough to write. That is planning, and planning is what a deep agent is for.

Sources that need authorization enforce it in their own tool layer, against
the principal that triggered the run, so this agent never learns the rules.
What a report contains therefore depends on who asked for it: the scheduled
service account sees across divisions, a user sees their own. See
``deep_research_agent.principals``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from langchain_core.tools import BaseTool

from deep_research_agent.protocol import AgentSpec
from deep_research_agent.runtime import DeepAgent, ToolContext, ToolsetFactory
from deep_research_agent.wiki.authz import WikiAuthorizer
from deep_research_agent.wiki.backend import WikiBackend
from deep_research_agent.wiki.tools import make_wiki_toolset
from deep_research_agent.report.sources import BomSource, NewsFeed
from deep_research_agent.report.tools import make_bom_toolset, make_news_toolset

__all__ = ["build_deep_research_agent", "SYSTEM_PROMPT", "AGENT_NAME"]

log = logging.getLogger(__name__)

AGENT_NAME = "deep_research_agent"

DEFAULT_SKILLS = ("incident-report",)
"""Skills inlined into the prompt. ``SKILLS_ENABLED`` adds to this."""


def _warn_about_unloaded_skills(project_root, loaded: list[str]) -> None:
    """Point out skills sitting in the repo that nothing is loading.

    The failure mode of a folder-based convention is a file that looks
    installed and silently is not: someone adds ``skills/house-style/`` for a
    scheduled job, never wires it up, and the job keeps running to the old
    rules with no signal. A warning is the cheapest way to close that gap
    without making every dropped-in folder mandatory policy.
    """
    from deep_research_agent.runtime import discover_skills

    unloaded = sorted(set(discover_skills(project_root)) - set(loaded))
    if unloaded:
        log.warning(
            "skills present but not loaded: %s. Add to DEFAULT_SKILLS "
            "(report/agent.py) or SKILLS_ENABLED to use them.",
            ", ".join(unloaded),
        )


def _env_skills(already: list[str]) -> list[str]:
    """Skills named by ``SKILLS_ENABLED``, minus any already requested.

    Additive rather than replacing: the report rubric is what makes the output
    publishable, so an env var that could silently drop it is a footgun.
    """
    from deep_research_agent.config import get_skills

    return [n for n in get_skills().enabled_names() if n not in already]

FINDINGS_DIR = "/findings"
"""Where investigators write their notes, and where the lead reads them from.

The virtual filesystem is shared between an agent and its subagents in both
directions (verified: a subagent's writes land in the parent's state), which
is what lets a wide sweep hand back detail without pushing it through the
parent's context window.
"""

INVESTIGATOR_TOOLS = (
    "get_bom_company",
    "search_news",
    "fetch_article",
    "wiki_search",
    "wiki_read_page",
)
"""Data-source tools the investigator gets. Filesystem tools are NOT listed
here and do not need to be: ``deepagents`` gives every subagent its own
``FilesystemMiddleware`` regardless of this list. So "read-only" here means
read-only with respect to the *wiki* — the boundary that carries authorization
— not with respect to the scratchpad, which the investigator is meant to
write to."""

GENERAL_PURPOSE_TOOLS = (
    "list_bom_companies",
    "get_bom_company",
    "search_news",
    "fetch_article",
    "wiki_search",
    "wiki_read_page",
)
"""Tools for the ``general-purpose`` subagent — every read tool, no write tool.

``deepagents`` auto-inserts a ``general-purpose`` subagent when the caller does
not declare one, and that auto-inserted version **inherits the main agent's
entire tool list, including ``wiki_write_page``**. That silently defeats two
invariants this agent is built on: that only the top-level agent publishes, and
that nothing reaches the wiki without passing the fact-checker first.

So we declare it ourselves, which overrides the automatic one. The capability
is worth keeping — delegating an arbitrary sub-question to a fresh context
window is genuinely useful for open-ended research — it just must not be able
to publish.

Same explicit-allowlist rule as ``INVESTIGATOR_TOOLS``: MCP tools are absent
because nothing tells us which of them mutate state."""

VERIFIER_TOOLS = (
    "fetch_article",
    "wiki_read_page",
    "get_bom_company",
)
"""The fact-checker re-reads primary sources. It gets no search tools: its job
is to check the claims in front of it, not to go find new ones."""


SYSTEM_PROMPT = """\
You are a deep research agent. You investigate open-ended questions
about our supply chain by pulling together everything available to you, and you
publish sourced findings.

# Your sources

Treat these as complementary; a finding from one is rarely enough on its own.

- **Bill of materials** (`list_bom_companies`, `get_bom_company`) — which
  companies we depend on, for which components, and under what aliases.
- **External news** (`search_news`, `fetch_article`) — what happened out in
  the world.
- **Internal wiki** (`wiki_search`, `wiki_read_page`, `wiki_write_page`) —
  what we already knew, and where finished reports get published.

# How to work

Plan before acting. State briefly what you are about to do, then do it. Use
your todo list to track a multi-company sweep so nothing is silently dropped.

When a sweep covers several companies, delegate them to `company-investigator`
subagents via the `task` tool — launch them in a single message so they run
concurrently. Each one writes its detail to `/findings/<company_id>.md` and
replies with a short summary. **Read those files with `read_file` when you
synthesise.** Their replies are pointers, not the evidence; the file has the
sources you need to cite. Use `ls` on `/findings` to see what came back.

Investigate directly, without delegating, when the task concerns one company
or a handful of lookups.

Your access to any source is scoped to whoever triggered this run. If a search
returns nothing, that may mean the record exists somewhere you cannot read —
say "no record visible to this run", not "no record exists". Never speculate
about the contents of something you were refused.

# Evidence discipline

This is the part that matters most. A report nobody can audit is worse than no
report.

- Distinguish "no signal found" from "no incident occurred". Say which one you
  mean, every time.
- Read an article before citing it. Headlines routinely overstate scope.
- Cross-reference every external finding against what we already know before
  calling it new. We have often seen the issue already, and the internal
  record usually changes the severity assessment. Search previews are
  truncated — read the page.
- Attach a source to every factual claim. If you cannot source it, drop it.
- Report faithfully: if you could not check something, say so plainly rather
  than implying coverage you do not have.
- **When two sources disagree, say so.** Do not quietly pick the one that
  reads better. An external report and our internal record contradicting each
  other is usually the most valuable thing in the report, not an
  inconvenience to smooth over — give both, with both sources, and say which
  you find more credible and why.

# Before you write: are you actually done?

Deep research fails in two directions — stopping at the first plausible
answer, and digging forever. Check yourself explicitly before drafting:

- Which companies in scope have no finding file? Those are gaps, not
  clean results. Either investigate them or list them under Coverage.
- Which claims rest on exactly one source? Either corroborate them or mark
  them as single-sourced in the report.
- What would change your severity call if you learned it? If that is cheap
  to check, check it.

State the answers in one or two lines, then draft. If nothing is missing, say
so and move on — this is a check, not a ritual.

# Verify before publishing

Once the report is drafted and before you publish it, hand it to the
`fact-checker` subagent along with the sources it rests on. It re-reads the
primary sources and returns a verdict per claim.

If it comes back REVISE, fix what it flagged and re-check. Do not publish over
an unresolved REVISE, and do not resolve one by deleting the claim's citation
— the fix for an unsupported claim is to drop the claim or find its source.

# Publishing

Publish with `wiki_write_page` only once the report is complete. `source_refs`
must list every raw report id and external URL the content rests on — the call
is rejected without them, and that rejection is correct.

A report drawing on more than one division is a cross-division artefact and
belongs in a clearance-gated namespace. If the write is refused for that
reason, the fix is to publish to the gated namespace, not to drop the sources
that triggered the check.

If publishing is refused on permissions, do not try to route around it: return
the finished report as your answer and say plainly that it was not published,
and why.

Keep your final message short: what you found, what changed, what needs a
human. The report itself lives on the published page, not in the chat.
"""


INVESTIGATOR_PROMPT = """\
You investigate one company for supply-chain incidents.

Work the problem in this order, and do not stop at the first search:

1. Look up the company's BOM entry to get its aliases and the components we
   source from it. Aliases matter — incidents are frequently reported under a
   parent company or brand name.
2. Search news under the legal name and each alias. One query is rarely enough.
3. Fetch the full text of anything that looks material. Judge from the body,
   not the headline.
4. Search internal records for history on this company or these components,
   and read in full anything that looks relevant. Prior context frequently
   changes the severity.

Vary your queries deliberately. A single phrasing finds a single slice of
what is out there, so work through: the legal name, each alias, the parent or
brand name, the component part numbers we buy, and incident words (recall,
outage, fire, strike, sanction, insolvency, breach). Stop when new queries
stop returning new material, not when the first one returns something.

# Write your findings to a file

Before you reply, write everything you found to `/findings/<company_id>.md`.
Include every source URL, the internal pages you read, the queries you ran,
and your severity reasoning — the long version, not a summary.

Then reply with a SHORT summary: severity, the one-line reason, and the file
path you wrote. The file is the record; your reply is the pointer. This is
what keeps a twenty-company sweep from overflowing the lead agent's context.

If you found nothing, still write the file, and say "no external signal found"
explicitly in both the file and your reply, listing the queries you tried, so
the caller can tell "checked, clean" from "not checked".
"""


GENERAL_PURPOSE_PROMPT = """\
You handle a self-contained sub-question the lead agent delegated to you, in
your own context window.

Work it properly rather than guessing: use the tools you have, read sources in
full before relying on them, and follow the same evidence rules as the rest of
this system — distinguish "no signal found" from "nothing happened", attach a
source to every factual claim, and say plainly what you could not check.

Write anything long to a file and reply with a short summary plus the path.
Your reply goes into the lead agent's context, so keep it tight.

You cannot publish. If the work you were given implies publishing, do the
research and hand it back — say so in your reply rather than looking for
another way to do it.
"""


VERIFIER_PROMPT = """You are a fact-checker. You are given a draft report and the sources it
claims to rest on. You do not write reports and you do not do new research.

For each factual claim in the draft, decide which of these it is:

- **Supported** — a cited source, read in full, actually says this.
- **Overstated** — the source says something weaker, narrower, or less
  certain than the draft does. This is the most common failure and the one
  you exist to catch.
- **Unsupported** — no cited source says it. Includes claims where a source
  is cited but does not contain the fact.
- **Contradicted** — a source says the opposite.

Re-read the sources. `fetch_article` and `wiki_read_page` are how you check;
do not judge from the draft's own summary of a source, which is exactly the
thing under test.

Reply with a list: the claim, the verdict, and for anything not `Supported`,
what the source actually says. Finish with an overall verdict of PASS (every
claim supported) or REVISE (anything else).

Be specific and be hard to please. A vague "looks fine" from you is worse
than useless, because it will be trusted.
"""


def build_deep_research_agent(
    *,
    bom: BomSource,
    news: NewsFeed,
    wiki_backend: WikiBackend,
    wiki_authz: WikiAuthorizer,
    project_root: str | Path,
    model: str | None = None,
    max_turns: int = 60,
    skills: Sequence[str] = DEFAULT_SKILLS,
    extra_toolsets: Sequence[ToolsetFactory] = (),
) -> DeepAgent:
    """Wire up the research agent with its sources, subagents, and report rubric.

    ``skills`` names the skills to inline into the system prompt (see
    ``runtime.py`` for why inlined rather than progressively disclosed). A name
    resolves across ``SKILLS_PATH`` and then this repo's ``skills/``;
    a path resolves literally. Anything in ``SKILLS_ENABLED`` is appended, so
    a deployment can add a skill it maintains elsewhere without editing code.

    ``extra_toolsets`` adds data sources beyond the three built in — MCP
    servers arrive this way (``deep_research_agent.mcp``).
    """

    resolved_skills = list(skills) + _env_skills(list(skills))
    _warn_about_unloaded_skills(project_root, resolved_skills)

    def subagents(ctx: ToolContext, tools: dict[str, BaseTool]) -> list[dict[str, Any]]:
        # Every subagent here is read-only with respect to the wiki: each is
        # handed a subset that simply does not contain the write tool, so it
        # cannot publish even if it decides it should.
        #
        # `general-purpose` MUST stay in this list. deepagents inserts its own
        # version when we do not, and that one inherits every tool the main
        # agent has -- wiki_write_page included. See GENERAL_PURPOSE_TOOLS.
        return [
            {
                "name": "general-purpose",
                "description": (
                    "Investigates a self-contained sub-question in its own "
                    "context window and reports back. Use it to keep a long "
                    "detour out of your own context. It can research but not "
                    "publish."
                ),
                "system_prompt": GENERAL_PURPOSE_PROMPT,
                "tools": [tools[n] for n in GENERAL_PURPOSE_TOOLS if n in tools],
            },
            {
                "name": "company-investigator",
                "description": (
                    "Investigates a single BOM company for external incidents and "
                    "cross-references internal history. Use one per company when "
                    "sweeping several companies."
                ),
                "system_prompt": INVESTIGATOR_PROMPT,
                # An explicit allowlist, not "everything except the write
                # tool": a tool added later is excluded until someone names it
                # here. That is why MCP tools do not reach the investigator --
                # nothing tells us which of them mutate state, and this
                # subagent must not be able to publish. Name one here to opt
                # it in once you know it is safe.
                "tools": [tools[n] for n in INVESTIGATOR_TOOLS if n in tools],
            },
            {
                "name": "fact-checker",
                "description": (
                    "Checks a drafted report's claims against the sources it "
                    "cites and returns PASS or REVISE with per-claim verdicts. "
                    "Run this after drafting and before publishing. It does no "
                    "new research."
                ),
                "system_prompt": VERIFIER_PROMPT,
                # Deliberately no search tools: a checker that can go find new
                # material tends to start researching instead of checking, and
                # a claim it "confirms" from a source the report never cited is
                # not the thing being verified.
                "tools": [tools[n] for n in VERIFIER_TOOLS if n in tools],
            },
        ]

    return DeepAgent(
        name=AGENT_NAME,
        description=(
            "Deep research over the supply chain: scans the bill of materials, "
            "connects external news to internal history, and publishes a sourced "
            "report. Call this for BOM sweeps and for ad-hoc 'what happened with "
            "<supplier>' questions that need real research, not just a lookup."
        ),
        system_prompt=SYSTEM_PROMPT,
        toolsets=[
            make_bom_toolset(bom),
            make_news_toolset(news),
            make_wiki_toolset(wiki_backend, wiki_authz, writable=True),
            *extra_toolsets,
        ],
        subagents=subagents,
        skills=resolved_skills,
        project_root=project_root,
        model=model,
        max_turns=max_turns,
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What to investigate and report on.",
                },
                "tier": {
                    "type": "string",
                    "description": "Optional BOM tier to restrict the sweep to.",
                },
                "publish": {
                    "type": "boolean",
                    "description": "Whether to publish the finished report. Default true.",
                },
            },
            "required": ["task"],
        },
    )


def report_spec(agent: DeepAgent) -> AgentSpec:
    """Convenience: the research agent as something another agent can call."""
    return agent.as_spec()
