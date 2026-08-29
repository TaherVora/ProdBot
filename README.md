# Error bot — POC

Fetches recent ERROR/CRITICAL logs from the `voc-trending` Cloud Run service,
dedups against past errors by embedding similarity, and for new errors
fetches relevant code from GitHub and asks OpenAI for a diagnosis + fix.

## Setup

1. **Postgres with pgvector** — any Postgres 13+ with the `vector` extension
   available (Cloud SQL supports it; local Docker works too for a POC):
   ```bash
   docker run -d --name errorbot-pg -e POSTGRES_PASSWORD=password \
     -p 5432:5432 pgvector/pgvector:pg16
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment**
   ```bash
   cp .env.example .env
   # fill in DATABASE_URL, OPENAI_API_KEY, GITHUB_TOKEN, GITHUB_REPO, GCP_PROJECT_ID
   ```

4. **Create the schema**
   ```bash
   python -c "from db import store; store.init_db()"
   ```

5. **Run it**
   ```bash
   python main.py
   ```

## Demo UI

```bash
streamlit run ui/app.py
```

Two tabs:
- **Submit an error** — pick a sample log (a 503 timeout with a full trace,
  a trace-less null-reference, and a fully generic error, one for each
  retrieval tier) or paste your own, and run it through the real pipeline.
  Shows whether it was a duplicate, adapted, or new (with the cosine
  distance), which retrieval tier grounded the fix, and the solution itself.
- **History** — every row in Postgres so far, most recent first, with the
  full solution and resolution/retrieval tier per error.

This calls the same `pipeline.process_log()` that `main.py` uses — no
separate demo logic, so what you see in the UI is exactly what the batch
job would have done.

## How it decides "duplicate" vs "adapted" vs "new"

Each error is normalized (`error_type | endpoint | message`) and embedded
with OpenAI (`text-embedding-3-small` by default), then compared against the
nearest existing row for the same service via cosine distance
(`db.store.find_similar_error`). What happens next depends on that distance
and whether the reported `filename` matches the matched row's:

1. **Duplicate** (distance ≤ `EXACT_MATCH_THRESHOLD`, `.env`, default `0.02`,
   AND same reported filename as the matched row) — occurrence count goes
   up, the stored solution is reused as-is, and no chat model is called at
   all.
2. **Adapted** (distance between `EXACT_MATCH_THRESHOLD` and
   `SIMILARITY_THRESHOLD`, OR within `EXACT_MATCH_THRESHOLD` but a
   *different* filename — e.g. the same error shape recurring in a new
   file) — one deterministic, non-agentic lookup (`github_agent.resolve_seed`,
   no LLM cost) tries to fetch the new file's own code, then a single
   tools-free call to a cheaper model (`OPENAI_ADAPT_MODEL`, default
   `gpt-4.1-mini`) adapts the matched row's solution to the new error/file.
   Grounded in the new file's real code when it can be resolved; otherwise
   falls back to a text-only, explicitly-flagged-as-unverified adaptation.
   Stored as a **new** row with `resolution_tier='adapted'` and
   `reference_error_id` pointing at the row it was adapted from.
3. **New** (distance above `SIMILARITY_THRESHOLD`, `.env`, default `0.15`,
   or no rows yet for this service) — the full agentic GitHub-retrieval
   diagnosis runs as before, using `OPENAI_CHAT_MODEL`. Stored with
   `resolution_tier='new'`.

**Tune the thresholds before trusting them.** Run the bot against a batch of
real logs, print the distances in `db.store.find_similar_error`, and eyeball
where true duplicates / near-duplicates / genuinely different errors land —
`0.02` and `0.15` are starting points, not calibrated values.

## Adding more services

Retrieval is scoped per-service via `service_repo_map` in `db/schema.sql`, keyed
on the Cloud Run service name (must match `resource.labels.service_name` in
the logs exactly). One row is seeded: `amaz-clone` → `your-org/amaz-clone`.

To onboard another service, add a row:
```sql
INSERT INTO service_repo_map (service_name, repo, path_prefix, default_branch)
VALUES ('checkout-service', 'your-org/checkout-service', NULL, 'main');
```

If a service has no row here, retrieval falls back to no code context
(`context_used = 'none'`) rather than guessing a repo — same principle as
the retrieval tiers themselves. Since all repos are in one org for now, a
single fine-grained `GITHUB_TOKEN` works; just make sure it's scoped to
every repo listed in this table, not the whole org, so a mapping mistake
can't accidentally read a repo it shouldn't.

Dedup (`db.store.find_similar_error`) is also scoped by `service_name`, so two
different services returning a similar generic error won't get deduped
against each other.

## How code retrieval works

A cheap deterministic seed step (`integrations/github.py`), then a bounded
agentic tool-calling loop (`integrations/github_agent.py`) that can pull in
more files than just the one the seed step found:

1. **Seed** — stack trace names a file directly → fetch that file; else a
   function/service name is present → one GitHub code search, capped to
   `MAX_SEARCH_RESULTS` files; else no seed file.
2. **Agentic loop** — the model gets read-only tools (`get_file_contents`,
   `find_file_by_name`, `list_directory`, `search_code`, all scoped to the
   one repo already resolved for this service) and decides what else it
   needs — repository/DTO files a fix also touches, files it has to search
   for because the seed step found nothing, in any language, not just
   JVM imports. Bounded by `MAX_TOOL_CALL_ROUNDS`, `MAX_FILES_PER_ERROR`, and
   `GITHUB_AGENT_TIMEOUT_SECONDS` so it can't run away.
3. If nothing was found at all, the model is told explicitly and gives a
   general, pattern-based suggestion rather than guessing at code it hasn't
   seen. Every file actually used is stored per-error in `source_files`
   (Postgres array column) and shown in the demo UI.

## Not in this POC (by design)

- Slack/email notification — results just print to stdout for now.
- Real MCP protocol / hosted GitHub MCP server — retrieval gives the model
  equivalent read-only tool-calling capability (search/list/fetch, scoped to
  one repo) without a separate MCP client/subprocess/transport; revisit if
  the tool surface needs to grow beyond read-only code retrieval (issues,
  PRs, commits, etc).