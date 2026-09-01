---
name: ai4bi-mcp-tool
description: >
  Build a new MCP tool in the AI4BI-MCPServices repo (FastMCP + streamable
  HTTP, SQLAlchemy over AiMi MariaDB / ParadeDB). Use when adding, debugging,
  or reviewing a tool under mcp_app/tools/ — especially a search or retrieval
  tool, where the characteristic failure is a call that succeeds and returns
  the wrong rows. Covers the repo's layout and config conventions, the
  verify-against-the-real-database-first workflow, ParadeDB BM25 scoring
  traps, the system_args authorization question, and the test ladder.
---

# Building an MCP tool in AI4BI-MCPServices

## The one thing that governs everything else

Retrieval tools do not fail loudly. They return a 200, a well-formed payload,
and the wrong rows. Every convention below exists because some specific
version of that happened:

- BM25 scoring that silently stops ranking → an arbitrary k of the matches
- a tokenizer that does not segment CJK → every Chinese query returns zero
- an authorization payload parsed wrong → empty results that read as "no records"
- a fallback to `ILIKE` → substring matching that every caller believes is relevance

So the working rule: **for anything that could be silently wrong, produce
evidence, not an argument.** Run the query, read the EXPLAIN, print the schema.
Do not reason from a library's documented behaviour when the database is one
command away.

## Repo layout

```
mcp_app/
  main.py                    FastMCP('CPOChat'), streamable_http_app(), uvicorn
  tools/
    __init__.py              tool_list -- a new tool is not mounted until it is here
    <tool_name>/
      __init__.py            the tool function itself
      schema.py              pydantic response models
  middleware/auth.py         KeycloakAuthMiddleware, on when auth_configs.enable
  prompts/
config/
  db.py                      one BaseSettings class per database
  __init__.py                instantiates them: db = DatabaseBase(), etc.
db/engine.py                 init/get engine + session maker, per database
shared/<db>/database_schema/models/    SQLAlchemy models (separate repo/submodule)
scripts/                     diagnostics and exercisers
```

- `main.py` registers via `mcp.add_tool(fn=tool_fn)` for each entry in
  `tool_list`, so the tool function's **signature and docstring are the public
  API** — FastMCP derives the JSON schema from them.
- Transport is streamable HTTP, `stateless_http=True`, mounted at `/mcp`.

## Conventions worth following

### Config

- One `BaseSettings` class per database, `env_prefix` naming it exactly
  (`aimi_paradedb_`, `aimi_mariadb_`). AiMi has more than one database; a
  prefix like `aimi_` alone is ambiguous and will eventually connect the wrong
  one.
- Type `port` as `int`. Reading it with `os.environ.get` yields a string,
  and `URL.create` wants an int — a mismatch that surfaces much later as a
  connection to the wrong place.
- `db/engine.py` must read the config class, not `os.environ`. A missed
  migration here produces the signature failure: **every value is `None`, so
  libpq falls back to a local Unix socket on 5432**, and the error looks like
  the database being down rather than the config not being wired.

### Response schemas

- Define pydantic models in `schema.py`; do not return bare dicts. The wrapper
  carries what no single row can say: what was actually searched, whether the
  result was cut short, and a `note` explaining an empty result.
- **Mirror the SQLAlchemy column types exactly.** The coercion is asymmetric:
  pydantic v2 turns `"42"` into an `int` field happily, but an `int` into a
  `str` field **raises**. Declaring an `Integer` column as `str` does not
  stringify it — it fails validation on every row.
- Return a wrapper, never a bare list. "No results" and "you have no readable
  namespaces" must not look the same to the caller.

### Query efficiency

- `select(Model)` pulls every column. Check the model first: one of these
  tables has a `Vector(4096)` embedding column that you do not want in a
  result set. Select the columns you use.

### Docstrings are the tool's UI

The function docstring becomes the description the calling agent reads. Say
what a parameter *does to the result*, not what type it is. Where a parameter
has a non-obvious interaction — ranking applied before sorting, a filter that
cannot widen access — say so there, because that is the only place the caller
will see it.

## The workflow that worked

1. **Write the tool against the models**, with every assumption stated as a
   comment where it is relied on.
2. **Write `scripts/check_<tool>_assumptions.py`** — read-only, exits non-zero
   on a blocker, so it doubles as a CI gate. Check each assumption separately
   and keep going after a failure; the point is to come away with every answer
   in one run. What it checked here: config values, whether `db/engine.py`
   agrees with the config class, tables exist and have rows, extensions
   installed, indexes present and covering the right columns, column types,
   both candidate query syntaxes, and the real values of the filter column.
3. **Fix what it reports**, including DDL and model changes (those go to the
   DBA repo as a PR).
4. **Write `scripts/try_<tool>.py`** — calls the function in-process, with any
   unresolved input (an account payload, say) supplied by hand so the unknown
   does not block testing everything downstream of it. Give it a `--selftest`
   that asserts the *properties that break silently*, not that the call
   returns.
5. **Write `scripts/mcp_client_demo.py`** — connects over the real transport
   and prints the tool's advertised input schema before calling. This answers
   transport-level questions that in-process testing cannot.

Each script answers questions the previous one cannot. Do not collapse them.

## The `system_args` question — resolve it, do not guess

Tool functions take `system_args: dict = None`. It is **not** a client-supplied
parameter; the server injects it. The existing convention is:

```python
# mcp_app/tools/doc_retrieval/ppd.py
authorized_topic_lower = [
    tpc.lower()
    for _, tpcs in system_args['authorized_topic'].items()
    for tpc in tpcs
]
```

`system_args['authorized_topic']` is `dict[domain, list[topic]]`. Also present:
`system_args['domain']`, `system_args.get('user_id')`.

To find the shape for a new tool:

```bash
grep -rn "system_args" mcp_app/ --include=*.py | grep -v "dict = None"
python scripts/mcp_client_demo.py --schema-only   # confirms it is not client-supplied
```

Two rules learned the hard way:

- **Parse it in exactly one function**, so the account shape is knowledge that
  lives in one place.
- **Fail closed.** An unrecognised payload returns *nothing*, never everything.
  Treating "I could not find the permissions" as "no restrictions" turns a
  parsing bug into a data leak that looks like the tool working.

And one question that is a product decision, not an implementation detail:
`authorized_topic` was designed for PPD document retrieval. Reusing it as wiki
namespace authorization asserts "may read PPD SecurityB documents = may read
PPD-SecurityB wiki pages". Confirm that before writing it.

## Search tools specifically

If the tool ranks results with ParadeDB BM25, read
`references/paradedb-bm25.md` before writing the SQL. It documents the scoring
trap that cost the most time here, with the EXPLAIN output that identifies it.

## Testing ladder

```bash
python scripts/check_<tool>_assumptions.py   # is the world what the code assumes?
python scripts/try_<tool>.py --selftest      # do the silent-failure properties hold?
python scripts/try_<tool>.py "<query>"       # what does it actually return?
python mcp_app/main.py                       # then, in another terminal:
python scripts/mcp_client_demo.py --schema-only
python scripts/mcp_client_demo.py "<query>"
```

Properties a `--selftest` should assert, because each of these fails silently:

| Property | What it catches |
|---|---|
| scores are not all identical | ranking is not happening; you have an arbitrary k |
| relevance order is descending | the ORDER BY is not doing what it looks like |
| no account → zero results | authorization failing open |
| an unreadable scope → zero results | a parameter widening access |
| a date sort returns the same page set as relevance | sorting is *selecting*, so you get the newest rows rather than the best matches |
| truncation flags report pre-truncation length | callers cannot tell they need to re-fetch |

Some things cannot be asserted automatically and must be run by hand with a
value you have confirmed exists — a CJK query is the standing example, because
"the tokenizer is misconfigured" and "nothing matches that word" produce
identical output.

## Reviewing a proposed fix

When a query misbehaves and a fix "works", check what "works" means. The
specific trap: `paradedb.score()` returned NULL, and replacing it with
`(page_id @@@ :query)` stopped the crash — because `@@@` returns **boolean**,
`float(True)` is `1.0`, and every row scored the same. The exception went
away and the ranking went with it.

Before accepting any fix to a ranking or filtering query, ask what the new
expression's *type* is, and whether the output could look healthy while being
wrong.
