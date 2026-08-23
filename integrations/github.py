"""
Tiered code retrieval — deliberately NOT an agentic/exploratory search.
Each tier is a bounded, deterministic step:

  Tier 1: the log payload names the exact file (jsonPayload.filename) ->
          fetch it directly if that name is already a repo-relative path,
          else resolve its real path by listing the repo's file tree (Git
          Trees API) and matching on filename locally (compiled-language
          apps like Java only ever report a bare filename, never a path).
          Deliberately NOT GitHub's /search/code endpoint for this — that's
          a search index which frequently omits smaller/personal repos
          entirely and silently returns zero hits, rather than reading the
          repo itself.
  Tier 2: no filename in the payload, but a function/class/service name is
          present in the message -> GitHub code search, capped to
          MAX_SEARCH_RESULTS files (best-effort; subject to the same
          indexing gaps noted above)
  Tier 3: nothing usable -> no code context at all (never falls back to
          scanning the whole repo)
"""

from __future__ import annotations

import base64
import logging
import re

import requests

import config

log = logging.getLogger(__name__)

_API = "https://api.github.com"
_HEADERS = {
    "Authorization": f"Bearer {config.GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

# A bare identifier: snake_case or camelCase function/class name, e.g.
# "fetchUserData" or "fetch_user_data" — used as a Tier 2 search query.
_IDENTIFIER_PATTERN = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]{3,})\b')
_STOPWORDS = {"error", "exception", "timeout", "failed", "traceback", "line"}

# Package-based languages (Java/Kotlin/Scala): used to find other files in the
# same project that the primary file depends on (repository, DTO, etc.), so
# the fix isn't scoped to just the one file the stack trace happens to name.
_PACKAGE_PATTERN = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.MULTILINE)
_IMPORT_PATTERN = re.compile(r'^\s*import\s+(?:static\s+)?([\w.]+)\s*;', re.MULTILINE)


def _extract_identifier(message: str) -> str | None:
    candidates = [
        w for w in _IDENTIFIER_PATTERN.findall(message or "")
        if w.lower() not in _STOPWORDS and not w.isupper()
    ]
    return candidates[0] if candidates else None


def _same_project_class_names(code: str) -> list[str]:
    """
    Class names imported by `code` that live in the same project, e.g. given
    package "com.taher.beerservice.service" and an import of
    "com.taher.beerservice.repository.BeerRepository", returns ["BeerRepository"].
    Stdlib/third-party imports (java.*, org.springframework.*, lombok.*, ...)
    are excluded because they aren't imports of *this* project's own root
    package. Java/Kotlin/Scala only for now (package-declaration languages);
    returns [] for anything else rather than guessing.
    """
    own_package = _PACKAGE_PATTERN.search(code)
    if not own_package:
        return []
    # First 3 dotted segments ~= Maven/Gradle groupId+artifactId convention
    # (e.g. "com.taher.beerservice"), so sibling packages like ".repository"
    # or ".web.controller" still count as the same project.
    root = ".".join(own_package.group(1).split(".")[:3])

    names = []
    for imp in _IMPORT_PATTERN.findall(code):
        if imp.startswith(root + ".") and "." in imp:
            names.append(imp.rsplit(".", 1)[-1])
    return names


def _gather_related_files(repo: str, branch: str, primary_path: str, primary_code: str,
                           path_prefix: str = None) -> list[dict]:
    ext = primary_path.rsplit(".", 1)[-1] if "." in primary_path else None
    if not ext:
        return []

    primary_filename = primary_path.rsplit("/", 1)[-1]
    class_names = _same_project_class_names(primary_code)
    log.info("repo=%s related class names from %s: %s", repo, primary_path, class_names)

    related = []
    for name in class_names:
        if len(related) >= config.MAX_RELATED_FILES:
            break
        filename = f"{name}.{ext}"
        if filename == primary_filename:
            continue
        paths = _find_file_by_name(repo, filename, branch, path_prefix)
        if not paths:
            continue
        code = _fetch_file(repo, paths[0])
        if code:
            related.append({"source_file": paths[0], "code": code})
    return related


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


def _code_search(scoped_query: str) -> list[str]:
    url = f"{_API}/search/code"
    resp = requests.get(url, headers=_HEADERS, params={"q": scoped_query}, timeout=10)
    if resp.status_code != 200:
        log.warning("code_search failed: q=%r -> %s %s", scoped_query, resp.status_code, resp.text[:300])
        return []
    items = resp.json().get("items", [])[:config.MAX_SEARCH_RESULTS]
    log.info("code_search ok: q=%r -> %d result(s): %s", scoped_query, len(items), [i["path"] for i in items])
    return [item["path"] for item in items]


def _search_by_identifier(repo: str, identifier: str, path_prefix: str = None) -> list[str]:
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


def _find_file_by_name(repo: str, filename: str, branch: str, path_prefix: str = None) -> list[str]:
    """Resolves a bare filename (e.g. from a Java/compiled-language trace, which
    never includes the source path) to its actual repo-relative path(s)."""
    paths = _list_repo_files(repo, branch)
    matches = [p for p in paths if p.rsplit("/", 1)[-1] == filename]
    if path_prefix:
        matches = [p for p in matches if p.startswith(path_prefix)]
    matches = matches[:config.MAX_SEARCH_RESULTS]
    log.info("find_file_by_name: repo=%s filename=%r branch=%s -> %s", repo, filename, branch, matches)
    return matches


def get_code_context(
    filename: str | None, message: str, repo: str = None, path_prefix: str = None, branch: str = None,
) -> dict:
    """
    Returns {"code": str|None, "source_file": str|None,
              "related_files": [{"source_file": str, "code": str}, ...]}

    `related_files` holds other same-project files the primary file imports
    (repository/DTO/etc.) — the fix may span more than just the file the log
    payload names, capped at MAX_RELATED_FILES.

    `repo`/`branch` come from the service -> repo mapping resolved by the caller.
    No repo means no service_repo_map entry for this service yet — treated
    the same as Tier 3 (no code context), never a fallback to some default repo.
    """
    if not repo:
        log.info("no repo mapping -> tier 3 (none)")
        return {"code": None, "source_file": None, "related_files": []}

    branch = branch or "main"

    # Tier 1: the log payload told us the filename. Try it as a repo-relative
    # path first (works if it's already a full path); if that 404s — or it's
    # a bare filename, as compiled-language apps always report — resolve the
    # real path by listing the repo's tree and matching on filename.
    log.info("repo=%s tier1 filename=%r", repo, filename)
    if filename:
        code = _fetch_file(repo, filename)
        if code:
            related = _gather_related_files(repo, branch, filename, code, path_prefix)
            return {"code": code, "source_file": filename, "related_files": related}

        base_filename = filename.rsplit("/", 1)[-1]
        paths = _find_file_by_name(repo, base_filename, branch, path_prefix)
        for path in paths:
            code = _fetch_file(repo, path)
            if code:
                related = _gather_related_files(repo, branch, path, code, path_prefix)
                return {"code": code, "source_file": path, "related_files": related}

    # Tier 2: search on a function/class/service identifier, capped results
    identifier = _extract_identifier(message)
    log.info("repo=%s tier2 extracted identifier=%r", repo, identifier)
    if identifier:
        paths = _search_by_identifier(repo, identifier, path_prefix)
        for path in paths:
            code = _fetch_file(repo, path)
            if code:
                related = _gather_related_files(repo, branch, path, code, path_prefix)
                return {"code": code, "source_file": path, "related_files": related}

    # Tier 3: nothing usable — do not guess, do not scan the repo
    log.info("repo=%s no usable context -> tier 3 (none)", repo)
    return {"code": None, "source_file": None, "related_files": []}