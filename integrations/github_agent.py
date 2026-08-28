"""
GitHub tool mechanics for the agentic retrieval+diagnosis loop — MCP-server-like
capabilities (search_code, get_file_contents, list_directory, find_file_by_name)
exposed as native OpenAI tool-calling functions, bound to the one repo already
resolved for this service. The model (driven from agent.py) decides what to
fetch, so it isn't limited to a single file or a single search term the way
integrations/github.py's deterministic Tier 1/2 seed is.

This module owns no LLM/OpenAI calls itself — it's pure GitHub-side mechanics
(tool schemas, tool dispatch, seed/tree helpers) that agent.py's diagnose()
orchestrates. Read-only by design: no write/mutating GitHub tools, no
cross-repo access (repo/branch/path_prefix are closed over, never
model-supplied).
"""

from __future__ import annotations

import logging

import config
from integrations import github

log = logging.getLogger(__name__)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_file_contents",
            "description": (
                "Fetch a file's full contents by exact repo-relative path (from a stack "
                "trace, an import, or a prior tool result). Request multiple files in "
                "the same round when you already expect to need them all."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repo-relative file path, e.g. 'src/service/UserService.java'",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_file_by_name",
            "description": (
                "Resolve a bare filename (no path) to its repo-relative path(s), e.g. "
                "from a Java/Kotlin stack trace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Bare filename, e.g. 'UserRepository.java'",
                    }
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List entries under a directory (non-recursive by default). Entries "
                "ending in '/' are subdirectories (recurse with another call); others "
                "are files, ready for get_file_contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path, e.g. 'src/main/java/com/acme/service'. Empty string for repo root.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, list all files under this path recursively. Defaults to false.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "Full-text code search (GitHub's search index, not live grep — may "
                "miss small/personal repos). MUCH lower rate limit than the other "
                "tools — prefer list_directory/find_file_by_name when you have any "
                "hint, use this only as a last resort, and never retry it if it fails."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search terms, e.g. an identifier name. Do not include repo: or path: qualifiers, those are added automatically.",
                    }
                },
                "required": ["query"],
            },
        },
    },
]


def _line_numbered(code: str) -> str:
    return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(code.splitlines()))


def _is_excluded(path: str) -> bool:
    if any(path.startswith(prefix) for prefix in config.TREE_EXCLUDE_PREFIXES):
        return True
    return path.rsplit("/", 1)[-1] in config.TREE_EXCLUDE_FILENAMES


def resolve_seed(filename: str | None, repo: str, path_prefix: str | None, branch: str) -> dict | None:
    """Deterministic Tier 1/2 seed fetch (integrations/github.py), line-numbered
    for citation. Returns {"source_file": str, "code": str} or None."""
    seed = github.get_code_context(filename, repo=repo, path_prefix=path_prefix, branch=branch)
    if seed.get("source_file") and seed.get("code"):
        return {"source_file": seed["source_file"], "code": _line_numbered(seed["code"])}
    return None


def fetch_filtered_tree(repo: str, branch: str, path_prefix: str | None = None) -> list[str]:
    """Full repo tree, minus build-tool/wrapper/doc noise (config.TREE_EXCLUDE_*)
    that could never plausibly be fetched for a diagnosis — cheap to trim before
    it ever reaches a prompt."""
    tree = github._list_repo_files(repo, branch)
    if path_prefix:
        tree = [p for p in tree if p.startswith(path_prefix)]
    return [p for p in tree if not _is_excluded(p)]


def _make_dispatch(repo: str, branch: str, path_prefix: str | None, state: dict, tree_cache: list[str] | None = None):
    _tree_cache: list[str] | None = tree_cache

    def _tree() -> list[str]:
        nonlocal _tree_cache
        if _tree_cache is None:
            _tree_cache = github._list_repo_files(repo, branch)
        return _tree_cache

    def get_file_contents(path: str) -> dict:
        code = github._fetch_file(repo, path)
        if code is None:
            return {"path": path, "error": "not found"}
        return {"path": path, "content": _line_numbered(code)}

    def find_file_by_name(filename: str) -> dict:
        matches = github._find_file_by_name(repo, filename, branch, path_prefix, paths=_tree())
        if not matches:
            return {"filename": filename, "matches": [], "error": "no matches"}
        return {"filename": filename, "matches": matches}

    def list_directory(path: str, recursive: bool = False) -> dict:
        prefix = path.strip("/")
        paths = _tree()
        if prefix:
            paths = [p for p in paths if p.startswith(prefix + "/") or p == prefix]
        if recursive:
            entries = list(paths)
        else:
            # _tree() only holds file (blob) paths, so collapse each match down to
            # its immediate child under `prefix` — a file's full path as-is, or a
            # subdirectory's full path with a trailing "/" — instead of filtering
            # to only paths with zero further nesting, which would hide every
            # subdirectory name and make it impossible to descend into the tree.
            entries: list[str] = []
            seen: set[str] = set()
            for p in paths:
                rest = p[len(prefix):].lstrip("/")
                if not rest:
                    continue
                head = rest.split("/", 1)[0]
                full = f"{prefix}/{head}" if prefix else head
                entry = f"{full}/" if "/" in rest else full
                if entry not in seen:
                    seen.add(entry)
                    entries.append(entry)
        entries = entries[: config.MAX_SEARCH_RESULTS]
        if not entries:
            return {"path": path, "entries": [], "error": "empty or not found"}
        return {"path": path, "entries": entries}

    def search_code(query: str) -> dict:
        rate_limited_detail = (
            "search_code is disabled for the rest of this diagnosis (GitHub rate limit) "
            "— use list_directory or find_file_by_name instead."
        )
        if state["search_disabled"]:
            return {"query": query, "results": [], "error": "rate_limited", "detail": rate_limited_detail}

        paths, error = github._search_by_identifier(repo, query, path_prefix)
        if error == "rate_limited":
            state["search_disabled"] = True
            log.warning("github_agent: repo=%s search_code disabled for the rest of this diagnosis (rate limit)", repo)
            return {"query": query, "results": [], "error": "rate_limited", "detail": rate_limited_detail}
        if error:
            return {"query": query, "results": [], "error": "search_failed"}
        if not paths:
            return {"query": query, "results": [], "error": "no_results"}
        return {"query": query, "results": paths}

    return {
        "get_file_contents": get_file_contents,
        "find_file_by_name": find_file_by_name,
        "list_directory": list_directory,
        "search_code": search_code,
    }
