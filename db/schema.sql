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
    embedding           vector(1536) NOT NULL,

    -- what the agent produced
    suggested_solution  TEXT,
    source_file         TEXT,               -- github path resolved from `filename`/search, if any
    source_files        TEXT[],             -- every file the retrieval step (seed + agentic tool loop) pulled in; source_file is source_files[1] when non-null

    -- cost-saving bookkeeping
    occurrence_count    INT NOT NULL DEFAULT 1,
    first_seen          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Safe to re-run against a database created before source_files existed
-- (CREATE TABLE IF NOT EXISTS above is a no-op once the table already exists).
ALTER TABLE error_logs ADD COLUMN IF NOT EXISTS source_files TEXT[];

-- Approximate nearest-neighbor index for cosine distance search.
-- ivfflat needs a rebuild (REINDEX) once you have real data volume; fine as-is for a POC.
CREATE INDEX IF NOT EXISTS error_logs_embedding_idx
    ON error_logs USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS error_logs_service_idx ON error_logs (service_name);