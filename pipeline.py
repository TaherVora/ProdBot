"""
The core loop from the architecture: embed -> dedup check -> (reuse | retrieve+generate).
Pulled out of main.py so the Streamlit demo can call the same code path
instead of re-implementing it.
"""

import logging

import agent
import config
import embeddings
from db import store as db
from integrations import github_agent

log = logging.getLogger(__name__)


def process_log(log_data: dict) -> dict:
    """
    log_data needs: raw_log, error_type, endpoint, filename, line, service_name
    (any may be None except raw_log).

    Returns a result dict describing what happened, shaped for either a
    print statement (main.py) or a UI render (app.py). `status` is one of
    "duplicate" (Tier 1), "adapted" (Tier 2), or "new" (Tier 3).
    """
    service_name = log_data.get("service_name")
    filename = log_data.get("filename")

    text = embeddings.normalize_error_text(
        log_data.get("error_type"), log_data.get("endpoint"), log_data["raw_log"]
    )
    embedding, embedding_tokens = embeddings.get_embedding(text)

    # Scoped to this service so two unrelated services don't dedup against
    # each other's similar-looking generic errors.
    match = db.find_similar_error(embedding, service_name=service_name)

    if match:
        # Tier 1 — exact duplicate: near-zero distance AND same reported
        # filename. filename isn't part of the embedded text, so the same
        # error shape recurring in a *different* file must not collapse into
        # Tier 1 — it falls through to Tier 2 below instead.
        if match["distance"] <= config.EXACT_MATCH_THRESHOLD and filename == match["filename"]:
            db.increment_occurrence(match["id"])
            log.info("process_log: service=%r status=duplicate tokens: embedding=%d total=%d",
                      service_name, embedding_tokens, embedding_tokens)
            return {
                "status": "duplicate",
                "matched_id": match["id"],
                "distance": match["distance"],
                "occurrence_count": match["occurrence_count"] + 1,
                "solution": match["suggested_solution"],
                "source_file": match["source_file"],
                "source_files": match["source_files"],
            }

        # Tier 2 — similar but not exact: adapt the matched row's solution
        # with a cheap model. One deterministic, non-agentic lookup for the
        # *new* error's own file (no LLM cost, no multi-round exploration)
        # grounds the adaptation when possible; falls back to text-only.
        mapping = db.get_repo_mapping(service_name)
        seed = None
        if mapping:
            seed = github_agent.resolve_seed(
                filename, mapping["repo"], mapping["path_prefix"], mapping["default_branch"]
            )

        result = agent.adapt_solution(
            raw_log=log_data["raw_log"],
            error_type=log_data.get("error_type"),
            endpoint=log_data.get("endpoint"),
            filename=filename,
            reference_solution=match["suggested_solution"],
            reference_source_file=match["source_file"],
            reference_source_files=match["source_files"],
            new_file_code=seed["code"] if seed else None,
            new_file_source=seed["source_file"] if seed else None,
            service_name=service_name,
        )
        solution = result["solution"]
        total_tokens = embedding_tokens + result["prompt_tokens"] + result["completion_tokens"]
        log.info(
            "process_log: service=%r status=adapted reference_id=%d distance=%.4f grounded=%s "
            "tokens: embedding=%d chat_prompt=%d chat_completion=%d total=%d",
            service_name, match["id"], match["distance"], bool(seed), embedding_tokens,
            result["prompt_tokens"], result["completion_tokens"], total_tokens,
        )

        new_id = db.insert_error(
            raw_log=log_data["raw_log"],
            error_type=log_data.get("error_type"),
            endpoint=log_data.get("endpoint"),
            filename=filename,
            line=log_data.get("line"),
            embedding=embedding,
            suggested_solution=solution,
            source_file=seed["source_file"] if seed else None,
            service_name=service_name,
            repo=mapping["repo"] if mapping else None,
            source_files=[seed["source_file"]] if seed else None,
            resolution_tier="adapted",
            reference_error_id=match["id"],
        )
        return {
            "status": "adapted",
            "id": new_id,
            "reference_id": match["id"],
            "distance": match["distance"],
            "solution": solution,
            "source_file": seed["source_file"] if seed else None,
            "source_files": [seed["source_file"]] if seed else None,
        }

    # Tier 3 — no match close enough (or no rows for this service yet):
    # unchanged full agentic GitHub-retrieval diagnosis.
    mapping = db.get_repo_mapping(service_name)
    repo = mapping["repo"] if mapping else None
    path_prefix = mapping["path_prefix"] if mapping else None
    default_branch = mapping["default_branch"] if mapping else None

    result = agent.diagnose(
        log_data["raw_log"], filename,
        repo=repo, path_prefix=path_prefix, branch=default_branch,
        service_name=service_name, reported_line=log_data.get("line"),
    )
    solution = result["solution"]
    source_files = [f["source_file"] for f in result["files"]]

    total_tokens = embedding_tokens + result["prompt_tokens"] + result["completion_tokens"]
    log.info(
        "process_log: service=%r status=new tokens: embedding=%d chat_prompt=%d chat_completion=%d total=%d",
        service_name, embedding_tokens, result["prompt_tokens"], result["completion_tokens"], total_tokens,
    )

    new_id = db.insert_error(
        raw_log=log_data["raw_log"],
        error_type=log_data.get("error_type"),
        endpoint=log_data.get("endpoint"),
        filename=filename,
        line=log_data.get("line"),
        embedding=embedding,
        suggested_solution=solution,
        source_file=result["primary_source_file"],
        service_name=service_name,
        repo=repo,
        source_files=source_files or None,
        resolution_tier="new",
        reference_error_id=None,
    )
    return {
        "status": "new",
        "id": new_id,
        "source_file": result["primary_source_file"],
        "source_files": source_files,
        "solution": solution,
        "repo": repo,
    }