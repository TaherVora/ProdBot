"""
The core loop from the architecture: embed -> dedup check -> (reuse | retrieve+generate).
Pulled out of main.py so the Streamlit demo can call the same code path
instead of re-implementing it.
"""

import logging

import agent
import embeddings
from db import store as db

log = logging.getLogger(__name__)


def process_log(log_data: dict) -> dict:
    """
    log_data needs: raw_log, error_type, endpoint, filename, line, service_name
    (any may be None except raw_log).

    Returns a result dict describing what happened, shaped for either a
    print statement (main.py) or a UI render (app.py).
    """
    service_name = log_data.get("service_name")

    text = embeddings.normalize_error_text(
        log_data.get("error_type"), log_data.get("endpoint"), log_data["raw_log"]
    )
    embedding, embedding_tokens = embeddings.get_embedding(text)

    # Scoped to this service so two unrelated services don't dedup against
    # each other's similar-looking generic errors.
    match = db.find_similar_error(embedding, service_name=service_name)
    if match:
        db.increment_occurrence(match["id"])
        log.info("process_log: service=%r status=duplicate tokens: embedding=%d total=%d",
                  service_name, embedding_tokens, embedding_tokens)
        return {
            "status": "duplicate",
            "matched_id": match["id"],
            "distance": match["distance"],
            "occurrence_count": match["occurrence_count"] + 1,
            "solution": match["suggested_solution"],
        }

    # Resolve which repo this service's code lives in. No mapping ->
    # no code context, same as Tier 3 — never guess a repo.
    mapping = db.get_repo_mapping(service_name)
    repo = mapping["repo"] if mapping else None
    path_prefix = mapping["path_prefix"] if mapping else None
    default_branch = mapping["default_branch"] if mapping else None

    result = agent.diagnose(
        log_data["raw_log"], log_data.get("filename"),
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
        filename=log_data.get("filename"),
        line=log_data.get("line"),
        embedding=embedding,
        suggested_solution=solution,
        source_file=result["primary_source_file"],
        service_name=service_name,
        repo=repo,
        source_files=source_files or None,
    )
    return {
        "status": "new",
        "id": new_id,
        "source_file": result["primary_source_file"],
        "source_files": source_files,
        "solution": solution,
        "repo": repo,
    }