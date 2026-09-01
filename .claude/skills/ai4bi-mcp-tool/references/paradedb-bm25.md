# ParadeDB BM25 in AI4BI-MCPServices

Everything here was established against the real database, not read from docs.
The EXPLAIN output is included because it is what makes the diagnosis checkable
rather than plausible.

## Index shape

The convention in this codebase (see the `news` table) is a BM25 index for the
text columns and **separate btree indexes for filter columns**:

```sql
CREATE INDEX <table>_bm25_fulltext ON <table>
USING bm25 (page_id, page_name, description)
WITH (
  key_field=page_id,                    -- no quotes around the column name
  text_fields='{
    "page_name":   {"tokenizer": {"type": "icu", "stemmer": "English"}},
    "description": {"tokenizer": {"type": "icu", "stemmer": "English"}}
  }'
);
```

- `key_field` takes the column **unquoted**.
- `key_field` must be the same column passed to `paradedb.score(...)`.
- The `icu` tokenizer is what makes CJK searchable at all — the default does
  not segment it, so a Chinese query matches nothing while the index looks
  healthy. `"stemmer": "English"` handles the Latin half of mixed content.
- In SQLAlchemy this is an `Index(..., postgresql_using="bm25",
  postgresql_with={...})` in `__table_args__`.

## The scoring trap

`paradedb.score()` returns a value **only when the planner runs the ParadeDB
custom scan.** Adding an ordinary predicate to the same WHERE clause can
demote the plan to a regular index scan over the BM25 index, where `@@@` still
matches correctly but scores come back NULL.

Broken — scores are NULL:

```sql
SELECT page_id, paradedb.score(page_id) AS score
FROM llm_wiki_page_indexes
WHERE page_id @@@ :q
  AND namespace = ANY(CAST(:namespaces AS text[]))   -- demotes the plan
ORDER BY score DESC
LIMIT :limit
```

Its EXPLAIN — note `Index Scan`, not `Custom Scan`, and the namespace as a
heap `Filter`:

```
->  Index Scan using llm_wiki_page_indexes_bm25_fulltext on llm_wiki_page_indexes
      Output: page_id, paradedb.score(page_id)
      Index Cond: (page_id @@@ '...'::paradedb.searchqueryinput)
      Filter: (namespace = ANY ('{PPD-SecurityB}'::text[]))
```

Working — isolate the BM25 scan in a **MATERIALIZED** CTE so no other
predicate can reach it:

```sql
WITH ranked AS MATERIALIZED (
    SELECT page_id, paradedb.score(page_id) AS score
    FROM llm_wiki_page_indexes
    WHERE page_id @@@ :bm25_query
    ORDER BY score DESC NULLS LAST
    LIMIT :prefetch
),
filtered AS (
    SELECT r.page_id, r.score
    FROM ranked r
    JOIN llm_wiki_page_indexes i ON i.page_id = r.page_id
    WHERE i.namespace = ANY(CAST(:namespaces AS text[]))
)
SELECT f.page_id, f.score,
       (SELECT count(*) FROM ranked)   AS window_size,
       (SELECT count(*) FROM filtered) AS matched_in_window
FROM filtered f
ORDER BY f.score DESC NULLS LAST
LIMIT :limit
```

Its EXPLAIN — the CTE runs the custom scan, and the `Top N Limit` is pushed
into Tantivy rather than fetched and trimmed:

```
CTE ranked
  ->  Parallel Custom Scan (ParadeDB Scan) on llm_wiki_page_indexes
        Index: llm_wiki_page_indexes_bm25_fulltext
        Exec Method: TopNScanExecState
        Scores: true
           Sort Field: paradedb.score()
           Top N Limit: 200
```

61ms → 18ms on the same query, as a side effect.

### Details that are load-bearing

- **`MATERIALIZED` cannot be dropped.** Since PG12 a CTE is inlined by
  default, and inlining pushes the filter back into the scan — the plan
  demotes again and the fix silently stops working.
- **`NULLS LAST`.** `ORDER BY x DESC` defaults to NULLS FIRST, so one unscored
  row sorts to the top and takes the whole `LIMIT` with it.
- **The prefetch window.** Ranking now happens before filtering, so a row that
  matches but ranks below the window is not returned. Say so in the response
  when the window filled up; a silently short answer is the failure mode.
- **The count subqueries force full window evaluation.** A materialized CTE is
  still consumed lazily, so without them the window stops at `:limit` rows and
  you cannot tell whether more matched. That is the latency you are buying the
  `truncated` signal with.

## Never do this

```sql
SELECT page_id, (page_id @@@ :bm25_query) AS score   -- WRONG
```

`@@@` returns **boolean**. The WHERE clause already filtered to matching rows,
so every row scores `true`; `float(True)` is `1.0` in Python, so nothing
crashes; `ORDER BY` has nothing to order on; and `LIMIT k` returns an
arbitrary k of the matches. It looks like a fix because the exception stops.

Two queries that settle it in ten seconds:

```sql
SELECT pg_typeof(page_id @@@ 'page_name:(nvidia)') FROM llm_wiki_page_indexes LIMIT 1;
-- boolean

SELECT page_id, (page_id @@@ 'page_name:(nvidia)') AS score
FROM llm_wiki_page_indexes WHERE page_id @@@ 'page_name:(nvidia)' LIMIT 10;
-- every row: t
```

## Query syntax

The string-query form is the one that works on this deployment:

```python
f"page_name:({terms}) OR description:({terms})"
```

The builder form (`paradedb.boolean(should => ARRAY[paradedb.match(...)])`)
exists in other pg_search versions. **If you switch, switch the SQL and the
query-builder together** — changing one searches for the literal string
`page_name:(foo)`, which raises no error and never matches.

### The default-field trap

EXPLAIN shows the parser running with `page_id` as the default field:

```
Human Readable Query: page_id:(page_name:(nvidia) OR description:(nvidia))
```

So any term arriving **without** a field prefix is searched against `page_id`
and matches nothing. This is why stripping query-syntax characters from user
input is a correctness measure, not just injection defence:

```python
_QUERY_SYNTAX_CHARS = re.compile(r"[+\-!(){}\[\]^\"~*?:\\/]")
```

Remove that and a user query containing a bare `:` becomes a field selector
that silently matches nothing.

## Known follow-up

Putting `namespace` inside the BM25 index makes the filter pushdownable, keeps
the custom scan, and removes the prefetch window entirely:

```sql
USING bm25 (page_id, page_name, description, namespace)
WITH (key_field=page_id, text_fields='{
  ...,
  "namespace": {"tokenizer": {"type": "keyword"}, "fast": true}
}');
```

`keyword`, not `icu`: a namespace is an identifier, and tokenizing
`PPD-SecurityB` into `ppd` + `securityb` would make it match namespaces it is
not.

Requires a model change and an index rebuild. **EXPLAIN it before switching the
SQL to the flat form** and confirm the plan is a custom scan with
`Scores: true` — assuming the pushdown happens is how the original bug arrived.
