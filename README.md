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
  Shows whether it was a duplicate (with the cosine distance) or new, which
  retrieval tier grounded the fix, and the solution itself.
- **History** — every row in Postgres so far, most recent first, with the
  full solution and retrieval tier per error.

This calls the same `pipeline.process_log()` that `main.py` uses — no
separate demo logic, so what you see in the UI is exactly what the batch
job would have done.

## How it decides "duplicate" vs "new"

Each error is normalized (`error_type | endpoint | message`) and embedded
with OpenAI (`text-embedding-3-small` by default). A new error is compared
against every stored error via cosine distance (`db.store.find_similar_error`).
Below `SIMILARITY_THRESHOLD` (`.env`, default `0.15`) it's treated as a
repeat: occurrence count goes up, the stored solution is reused, and the
chat model is never called. Above threshold, it's genuinely new and goes
through the full pipeline.

**Tune the threshold before trusting it.** Run the bot against a batch of
real logs, print the distances in `db.store.find_similar_error`, and eyeball
where true duplicates vs. genuinely different errors land — 0.15 is a
starting point, not a calibrated value.

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