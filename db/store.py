"""
Postgres + pgvector access layer.

Two operations matter for the dedup flow:
  - find_similar_error(): nearest existing row by cosine distance
  - insert_error() / increment_occurrence(): the two ways a log gets written
"""

from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

import config

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection():
    conn = psycopg.connect(config.DATABASE_URL, autocommit=True)
    register_vector(conn)
    return conn


def init_db():
    """Run schema.sql. Safe to call repeatedly (uses IF NOT EXISTS)."""
    with open(_SCHEMA_PATH) as f:
        schema = f.read()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema)


def get_repo_mapping(service_name: str):
    """
    Looks up which repo a service's code lives in. Returns None if the
    service hasn't been mapped yet — callers should treat that as "no code
    context available" (Tier 3), never guess a repo.
    """
    if not service_name:
        return None
    query = """
        SELECT repo, path_prefix, default_branch
        FROM service_repo_map
        WHERE service_name = %s;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (service_name,))
            row = cur.fetchone()
    if row is None:
        return None
    repo, path_prefix, default_branch = row
    return {"repo": repo, "path_prefix": path_prefix, "default_branch": default_branch}


def find_similar_error(embedding, service_name: str = None, threshold: float = None):
    """
    Returns the closest existing row if its cosine distance is under `threshold`,
    else None. Distance 0 = identical, larger = less similar.

    Scoped to service_name when provided — two different services returning a
    similar-looking generic error (e.g. "connection timed out") shouldn't be
    deduped against each other.
    """
    threshold = threshold if threshold is not None else config.SIMILARITY_THRESHOLD

    if service_name:
        query = """
            SELECT id, suggested_solution, occurrence_count,
                   embedding <=> %s::vector AS distance
            FROM error_logs
            WHERE service_name = %s
            ORDER BY distance ASC
            LIMIT 1;
        """
        params = (embedding, service_name)
    else:
        query = """
            SELECT id, suggested_solution, occurrence_count,
                   embedding <=> %s::vector AS distance
            FROM error_logs
            ORDER BY distance ASC
            LIMIT 1;
        """
        params = (embedding,)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()

    if row is None:
        return None

    error_id, suggested_solution, occurrence_count, distance = row
    if distance > threshold:
        return None

    return {
        "id": error_id,
        "suggested_solution": suggested_solution,
        "occurrence_count": occurrence_count,
        "distance": distance,
    }


def increment_occurrence(error_id: int):
    query = """
        UPDATE error_logs
        SET occurrence_count = occurrence_count + 1,
            last_seen = now()
        WHERE id = %s;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (error_id,))


def list_errors(limit: int = 100):
    query = """
        SELECT id, service_name, repo, raw_log, error_type, endpoint, filename, line,
               source_file, source_files, suggested_solution, occurrence_count, first_seen, last_seen
        FROM error_logs
        ORDER BY last_seen DESC
        LIMIT %s;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (limit,))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_error(raw_log, error_type, endpoint, filename, line, embedding,
                  suggested_solution, source_file,
                  service_name=None, repo=None, source_files=None):
    query = """
        INSERT INTO error_logs
            (service_name, repo, raw_log, error_type, endpoint, filename, line,
             embedding, suggested_solution, source_file, source_files)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (
                service_name, repo, raw_log, error_type, endpoint, filename, line,
                embedding, suggested_solution, source_file, source_files,
            ))
            return cur.fetchone()[0]