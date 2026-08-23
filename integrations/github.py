"""
Deterministic fast-path code retrieval — the cheap seed step that runs
before the agentic tool-calling loop in integrations/github_agent.py.

The log payload names the exact file (jsonPayload.filename) -> fetch it
directly if that name is already a repo-relative path, else resolve its real
path by listing the repo's file tree (Git Trees API) and matching on
filename locally (compiled-language apps like Java only ever report a bare
filename, never a path). Deliberately NOT GitHub's /search/code endpoint for
this — that's a search index which frequently omits smaller/personal repos
entirely and silently returns zero hits, rather than reading the repo
itself; it also has a much tighter rate limit than the Trees/Contents APIs
used here, so it isn't worth spending on a single regex-guessed identifier
before the agentic loop even starts (a prior version of this module did
exactly that as a "Tier 2" and it reliably burned the search rate limit on
a low-value query like "http" before the model got a turn — removed).

If no filename resolves, integrations/github_agent.py's tool-calling loop
takes over — it can search, list directories, and fetch files the model
itself decides it needs (including chasing multi-file fixes across any
language, and building much better search queries than a single regex
token), rather than this module ever falling back to scanning the repo.
"""

from __future__ import annotations

import base64
import logging

import requests

import config

log = logging.getLogger(__name__)

_API = "https://api.github.com"
_HEADERS = {
    "Authorization": f"Bearer {config.GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def _fetch_file(repo: str, path: str) -> str | None:
    url = f"{_API}/repos/{repo}/contents/{path}"
    resp = requests.get(url, headers=_HEADERS, timeout=10)
    if resp.status_code != 200:
        log.warning("fetch_file failed: %s -> %s %s", url, resp.status_code, resp.text[:300])
        return None
    content = resp.json().get("content", "")
    decoded = base64.b64decode(content).decode("utf-8", errors="replace")
    log.info("fetch_file ok: %s (%d chars)", url, len(decoded))
    return decoded[:config.MAX_FILE_CHARS]


def _code_search(scoped_query: str) -> tuple[list[str], str | None]:
    """
    Returns (paths, error). `error` is None on a genuine 200 response (even
    if `paths` ends up empty — that's a real zero-result search), or a short
    machine-readable reason ("rate_limited" / "search_failed") when GitHub
    didn't actually answer the query at all. Callers that hand results back
    to an LLM (integrations/github_agent.py) need this distinction: "no
    results" invites retrying with a different query, "rate_limited" doesn't
    — retrying just burns another request against the same limit.
    """
    url = f"{_API}/search/code"
    resp = requests.get(url, headers=_HEADERS, params={"q": scoped_query}, timeout=10)
    if resp.status_code != 200:
        log.warning("code_search failed: q=%r -> %s %s", scoped_query, resp.status_code, resp.text[:300])
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            return [], "rate_limited"
        return [], "search_failed"
    items = resp.json().get("items", [])[:config.MAX_SEARCH_RESULTS]
    log.info("code_search ok: q=%r -> %d result(s): %s", scoped_query, len(items), [i["path"] for i in items])
    return [item["path"] for item in items], None


def _search_by_identifier(repo: str, identifier: str, path_prefix: str = None) -> tuple[list[str], str | None]:
    scoped_query = f"{identifier} repo:{repo}"
    if path_prefix:
        scoped_query += f" path:{path_prefix}"
    return _code_search(scoped_query)


def _list_repo_files(repo: str, branch: str) -> list[str]:
    """Full file listing for one branch via the Git Trees API — reads the repo
    directly rather than relying on GitHub's search index (see module docstring)."""
    url = f"{_API}/repos/{repo}/git/trees/{branch}"
    resp = requests.get(url, headers=_HEADERS, params={"recursive": "true"}, timeout=10)
    if resp.status_code != 200:
        log.warning("list_repo_files failed: repo=%s branch=%s -> %s %s",
                    repo, branch, resp.status_code, resp.text[:300])
        return []
    body = resp.json()
    if body.get("truncated"):
        log.warning("list_repo_files: repo=%s branch=%s tree truncated by GitHub (repo too large)", repo, branch)
    paths = [item["path"] for item in body.get("tree", []) if item.get("type") == "blob"]
    log.info("list_repo_files ok: repo=%s branch=%s -> %d file(s)", repo, branch, len(paths))
    return paths


def _find_file_by_name(
    repo: str, filename: str, branch: str, path_prefix: str = None, paths: list[str] = None,
) -> list[str]:
    """Resolves a bare filename (e.g. from a Java/compiled-language trace, which
    never includes the source path) to its actual repo-relative path(s)).
    Pass `paths` (an already-fetched tree listing) to skip a redundant
    _list_repo_files call — e.g. integrations/github_agent.py's tool loop
    reuses one cached tree across every find_file_by_name/list_directory
    call in a diagnosis instead of refetching it each time."""
    paths = paths if paths is not None else _list_repo_files(repo, branch)
    matches = [p for p in paths if p.rsplit("/", 1)[-1] == filename]
    if path_prefix:
        matches = [p for p in matches if p.startswith(path_prefix)]
    matches = matches[:config.MAX_SEARCH_RESULTS]
    log.info("find_file_by_name: repo=%s filename=%r branch=%s -> %s", repo, filename, branch, matches)
    return matches


def get_code_context(
    filename: str | None, repo: str = None, path_prefix: str = None, branch: str = None,
) -> dict:
    """
    Returns {"code": str|None, "source_file": str|None}

    This is only the deterministic fast-path seed — a direct filename fetch,
    if one resolves. integrations/github_agent.py's tool-calling loop is
    responsible for everything else, including searching (with queries the
    model builds itself, not a single regex-extracted token).

    `repo`/`branch` come from the service -> repo mapping resolved by the caller.
    No repo means no service_repo_map entry for this service yet — treated
    the same as no filename match, never a fallback to some default repo.
    """
    if not repo:
        log.info("no repo mapping -> no context")
        return {"code": None, "source_file": None}

    branch = branch or "main"

    # The log payload told us the filename. Try it as a repo-relative path
    # first (works if it's already a full path); if that 404s — or it's a
    # bare filename, as compiled-language apps always report — resolve the
    # real path by listing the repo's tree and matching on filename.
    log.info("repo=%s seed filename=%r", repo, filename)
    if filename:
        code = _fetch_file(repo, filename)
        if code:
            return {"code": code, "source_file": filename}

        base_filename = filename.rsplit("/", 1)[-1]
        paths = _find_file_by_name(repo, base_filename, branch, path_prefix)
        for path in paths:
            code = _fetch_file(repo, path)
            if code:
                return {"code": code, "source_file": path}

    log.info("repo=%s no usable seed -> agentic loop takes over", repo)
    return {"code": None, "source_file": None}