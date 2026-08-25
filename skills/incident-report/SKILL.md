---
name: incident-report
description: >
  Format and severity rubric for supply-chain incident reports built from a BOM
  scan. Defines the required page structure, the severity scale, and the
  provenance rules a report must satisfy before it can be published.
---

# Supply-chain incident report

## Severity rubric

Assign one level per company. When evidence is thin, pick the lower level and
say what would raise it.

| Level | Meaning | Typical evidence |
|---|---|---|
| `critical` | Supply of a component we depend on is stopped or will stop | Site destroyed, insolvency filing, export ban naming the company |
| `high` | Supply is materially at risk within a quarter | Extended outage, recall covering a component we buy, sanctions on a parent |
| `medium` | Credible disruption with unclear reach | Regional disruption at one of several sites, labour action, breach with unknown blast radius |
| `low` | Noted, no supply impact expected | Leadership change, litigation unrelated to production, minor breach |
| `none` | No signal found this cycle | Searches returned nothing material |

`none` is a finding. Record it with the queries you ran, so the next cycle can
tell "we checked and found nothing" apart from "we did not check".

## Page structure

Publish to the reporting division's namespace with slug
`incident-report-YYYY-WW`. Title: `Supply chain incident report — week NN, YYYY`.

```markdown
# Supply chain incident report — week NN, YYYY

## Summary
Two or three sentences. Lead with anything at `high` or `critical`; if there is
nothing above `medium`, say that in the first sentence.

## Findings

### <Company name> — <severity>
- **What happened:** one or two sentences, sourced.
- **Components affected:** from the BOM entry.
- **Internal context:** what the wiki already knew, linked. Write "no
  internal record" when there is none.
- **Assessment:** why this severity and not the one above or below it.
- **Sources:** every source backing the above, as markdown links.

### <Company name> — none
- **Queries run:** the alias terms searched.
- **Result:** no external signal found.

## Coverage
Companies scanned, companies skipped and why. A reader must be able to tell
what this report does *not* cover.

## Open questions
Anything needing a human. Empty section is fine; delete it rather than padding.
```

## Provenance rules

1. Every claim in **Findings** carries a source. No source, no claim.
2. **Every entry on a `Sources:` line is a markdown link**, `[name](target)`.
   News links to the article URL; a wiki page links to its route. Build them
   with `format_reference` — pass every reference for the section at once and
   paste back what it returns. Writing the link by hand gets the name wrong,
   because the name has to be the document's as the source returned it.
3. **Raw report ids never appear in the page.** They are provenance, not
   something a reader can follow. They go in `source_refs` on the publish
   call, where the aggregation check reads them; in the body, cite the wiki
   page they back instead.
4. `source_refs` on the publish call lists every source used anywhere on the
   page, raw report ids included. The publish is rejected without it.
5. Never cite an article you have not fetched in full.
6. Publishing is blocked until the exact text you intend to publish has passed
   `check_references`. Edit the draft afterwards and it must pass again.

## Writing

Lead with the outcome; supporting detail after. Plain sentences, terms spelled
out — the reader was not watching you work and does not know the shorthand you
built up along the way. Do not pad the page with filler sections; delete a
heading rather than write "nothing to report here" under it, except in
**Coverage**, where absence is the point.
