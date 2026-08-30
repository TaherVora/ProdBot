CREATE EXTENSION IF NOT EXISTS vector;

-- Maps a Cloud Run service to the GitHub repo its code lives in.
-- One row per service. Add more rows here as you onboard services being
-- monitored — the lookup key is whatever shows up in the log's
-- resource.labels.service_name, so it must match exactly.
CREATE TABLE IF NOT EXISTS service_repo_map (
    service_name    TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,       -- "org/repo" — same org for all rows today
    path_prefix     TEXT,                -- set this if repo is a monorepo shared by multiple services
    default_branch  TEXT NOT NULL DEFAULT 'main',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed mapping. Add one INSERT per service as you bring it under monitoring —
-- e.g. ('checkout-service', 'your-org/checkout-service', NULL, 'main').
INSERT INTO service_repo_map (service_name, repo, path_prefix, default_branch)
VALUES
    ('cricket-fever', 'TaherVora/cricket-fever', NULL, 'master'),
    ('beer-service', 'TaherVora/Beer-service', NULL, 'master')
ON CONFLICT (service_name) DO NOTHING;


CREATE TABLE IF NOT EXISTS error_logs (
    id                  SERIAL PRIMARY KEY,

    -- what came in
    service_name        TEXT,               -- Cloud Run service, joins to service_repo_map
    repo                TEXT,               -- resolved repo at the time this was processed (audit trail)
    raw_log             TEXT NOT NULL,
    error_type          TEXT,               -- httpRequest.status if present, else log severity
    endpoint            TEXT,               -- best-effort: route/service/function involved
    filename            TEXT,               -- bare filename reported by the app (jsonPayload.filename)
    line                INT,                -- line number reported by the app (jsonPayload.line)

    -- dedup
    embedding           vector(3072) NOT NULL,  -- text-embedding-3-large

    -- what the agent produced
    suggested_solution  TEXT,
    source_file         TEXT,               -- github path resolved from `filename`/search, if any
    source_files        TEXT[],             -- every file the retrieval step (seed + agentic tool loop) pulled in; source_file is source_files[1] when non-null

    -- dedup tier audit trail
    resolution_tier     TEXT NOT NULL DEFAULT 'new',  -- 'exact_duplicate' | 'adapted' | 'new'
    reference_error_id  INT REFERENCES error_logs(id), -- for 'adapted' rows, the row whose solution was adapted

    -- cost-saving bookkeeping
    occurrence_count    INT NOT NULL DEFAULT 1,
    first_seen          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- No vector index on `embedding` at 3072-dim (text-embedding-3-large):
-- pgvector's ivfflat/hnsw indexes cap out at 2000 dimensions for the plain
-- `vector` type (storage itself has no such limit, only indexing does).
-- Cosine search falls back to a sequential scan + sort, which is fine at POC
-- row counts. To get an index back at this dimension you'd need pgvector's
-- `halfvec` type (0.7.0+) with an HNSW/ivfflat index on halfvec_cosine_ops —
-- worth it only once real data volume makes the sequential scan slow.

CREATE INDEX IF NOT EXISTS error_logs_service_idx ON error_logs (service_name);