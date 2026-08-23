"""
The core loop from the architecture: embed -> dedup check -> (reuse | retrieve+generate).
Pulled out of main.py so the Streamlit demo can call the same code path
instead of re-implementing it.
"""

import agent
import embeddings
from db import store as db
from integrations import github as github_context


def process_log(log: dict) -> dict:
    """
    log needs: raw_log, error_type, endpoint, filename, line, service_name (any
    may be None except raw_log).

    Returns a result dict describing what happened, shaped for either a
    print statement (main.py) or a UI render (app.py).
    """
    service_name = log.get("service_name")

    text = embeddings.normalize_error_text(
        log.get("error_type"), log.get("endpoint"), log["raw_log"]
    )
    embedding = embeddings.get_embedding(text)

    # Scoped to this service so two unrelated services don't dedup against
    # each other's similar-looking generic errors.
    match = db.find_similar_error(embedding, service_name=service_name)
    if match:
        db.increment_occurrence(match["id"])
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

    context = github_context.get_code_context(
        log.get("filename"), log["raw_log"],
        repo=repo, path_prefix=path_prefix, branch=default_branch,
    )
    solution = agent.generate_solution(
        log["raw_log"], context["code"], context["source_file"],
        related_files=context["related_files"], service_name=service_name,
        reported_line=log.get("line"),
    )

    new_id = db.insert_error(
        raw_log=log["raw_log"],
        error_type=log.get("error_type"),
        endpoint=log.get("endpoint"),
        filename=log.get("filename"),
        line=log.get("line"),
        embedding=embedding,
        suggested_solution=solution,
        source_file=context["source_file"],
        service_name=service_name,
        repo=repo,
    )
    return {
        "status": "new",
        "id": new_id,
        "source_file": context["source_file"],
        "solution": solution,
        "repo": repo,
    }