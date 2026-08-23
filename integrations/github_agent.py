"""
Agentic GitHub code retrieval — MCP-server-like capabilities (search_code,
get_file_contents, list_directory, find_file_by_name) exposed to the model
as native OpenAI tool-calling functions, bound to the one repo already
resolved for this service. The model drives a bounded loop deciding what to
fetch, so it isn't limited to a single file or a single search term the way
integrations/github.py's deterministic Tier 1/2 seed is.

Read-only by design: no write/mutating GitHub tools, no cross-repo access
(repo/branch/path_prefix are closed over, never model-supplied). Bounded by
config.MAX_TOOL_CALL_ROUNDS, config.MAX_FILES_PER_ERROR, and
config.GITHUB_AGENT_TIMEOUT_SECONDS so a slow model or a chatty repo can
never stall the caller (main.py's batch loop or ingest/main.py's synchronous
Pub/Sub push handler) indefinitely — hitting a bound just ends the loop with
whatever files were already gathered; it never raises.
"""

from __future__ import annotations

import json
import logging
import time

import config
from integrations import github
from integrations.llm_client import client as _client

log = logging.getLogger(__name__)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_file_contents",
            "description": (
                "Fetch the full contents of a file at an exact repo-relative path. "
                "Use this once you know (or are trying) an exact path, e.g. from a "
                "stack trace, an import statement you read in another file, or a "
                "previous list_directory/find_file_by_name/search_code result."
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
                "Find the repo-relative path(s) of a file when you only know its bare "
                "filename (e.g. from a Java/Kotlin stack trace, which never includes "
                "the source path). Lists the full repo tree and matches on basename."
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
                "List entries under a directory path in the repo (non-recursive by "
                "default). Entries ending in '/' are subdirectories — call "
                "list_directory again on those to go deeper; entries without a "
                "trailing '/' are files — pass those straight to get_file_contents. "
                "Use this to explore project structure when you don't know an exact "
                "filename, e.g. to see what's in the same package/directory as a "
                "file you already have."
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
                "Full-text search for code across the repo using GitHub's code search "
                "(an index, not a live grep — may miss results in small/personal "
                "repos). This endpoint has a MUCH lower rate limit than the other "
                "tools here — prefer list_directory or find_file_by_name whenever you "
                "have any name/path hint to go on, and only reach for search_code when "
                "there's genuinely nothing else to try. If it fails once, do not retry "
                "it — switch to list_directory or find_file_by_name instead."
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

_SYSTEM_PROMPT = """You are gathering source code to diagnose a production error. You have \
read-only tools to fetch files, list directories, find files by name, and search code in one \
repository. Decide what files, if any, you need beyond what's already been fetched to fully \
diagnose this error and any fix that may span multiple files — a fix is often not limited to \
the single file a stack trace names (e.g. a repository method signature or a DTO field may \
also need to change).

search_code hits GitHub's code-search API, which has a much lower rate limit than \
list_directory/find_file_by_name/get_file_contents (those use a separate, far more generous \
API). Prefer list_directory or find_file_by_name whenever you have any name/path hint at all; \
use search_code only when there's nothing else to try, and never retry it more than once per \
diagnosis — if it fails, switch tools instead of trying a different search term.

When you have enough context, or your tools aren't turning up anything more useful, stop \
calling tools and reply with a brief plain-text note (it will be discarded — only the files \
you fetched via get_file_contents matter)."""


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
        return {"path": path, "content": code}

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


def gather_code_context(
    filename: str | None, message: str, repo: str = None, path_prefix: str = None, branch: str = None,
    service_name: str | None = None,
) -> dict:
    """
    Returns {"files": [{"source_file": str, "code": str}, ...], "primary_source_file": str|None,
              "prompt_tokens": int, "completion_tokens": int}

    Runs integrations/github.py's deterministic filename-based fast path first
    (cheap, no LLM round trip), then hands off to a bounded tool-calling loop
    so the model can search/explore/fetch whatever additional files a fix
    needs — across any language, with queries it builds itself rather than a
    single regex-extracted token. `prompt_tokens`/`completion_tokens` are the
    running total across every round of that loop, so callers can roll this
    into a per-error token total alongside agent.generate_solution's cost.
    """
    if not repo:
        log.info("github_agent: no repo mapping -> no context, no tool loop")
        return {"files": [], "primary_source_file": None, "prompt_tokens": 0, "completion_tokens": 0}

    branch = branch or "main"

    seed = github.get_code_context(filename, repo=repo, path_prefix=path_prefix, branch=branch)
    files: list[dict] = []
    seen_paths: set[str] = set()
    primary_source_file = seed.get("source_file")
    if primary_source_file and seed.get("code"):
        files.append({"source_file": primary_source_file, "code": seed["code"]})
        seen_paths.add(primary_source_file)

    state = {"search_disabled": False}
    context_header = f"Service: {service_name or 'unknown'}\nError log:\n{message}"

    # Always fetch the tree and hand it to the model directly — a seed file
    # match isn't proof the seed is actually relevant to the error (e.g. a
    # bare filename from a log can resolve to an unrelated bootstrap/config
    # class), so the model needs the full listing either way to pick further
    # files itself instead of discovering repo structure blind, one
    # list_directory/find_file_by_name round at a time.
    tree = github._list_repo_files(repo, branch)
    if path_prefix:
        tree = [p for p in tree if p.startswith(path_prefix)]
    dispatch = _make_dispatch(repo, branch, path_prefix, state, tree_cache=tree)

    tree_listing = "\n".join(tree[: config.MAX_TREE_FILES_IN_PROMPT])
    truncation_note = ""
    if len(tree) > config.MAX_TREE_FILES_IN_PROMPT:
        truncation_note = f"\n... ({len(tree) - config.MAX_TREE_FILES_IN_PROMPT} more file(s) not shown)"

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    if files:
        seed_text = "\n\n".join(
            f"Already fetched via filename match ({f['source_file']}):\n{f['code']}" for f in files
        )
        messages.append({
            "role": "user",
            "content": f"{context_header}\n\n{seed_text}\n\nRepo file listing — if the file(s) above "
                       f"don't fully explain or fix this error, pick any other file(s) you need "
                       f"straight from here with get_file_contents using the exact path shown, "
                       f"rather than exploring blind:\n{tree_listing}{truncation_note}",
        })
    else:
        # No filename resolved — don't make the model guess filenames blind (it
        # has no idea what language/framework this repo even uses).
        messages.append({
            "role": "user",
            "content": f"{context_header}\n\nNo file could be resolved automatically from the "
                       f"filename. Here is the repo's file listing — pick the file(s) most likely "
                       f"relevant to this error (e.g. a controller/handler matching the route in "
                       f"the log) and fetch them with get_file_contents using the exact path shown "
                       f"below, rather than guessing filenames:\n{tree_listing}{truncation_note}",
        })

    start = time.monotonic()
    rounds = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    while rounds < config.MAX_TOOL_CALL_ROUNDS:
        if time.monotonic() - start > config.GITHUB_AGENT_TIMEOUT_SECONDS:
            log.warning("github_agent: repo=%s timed out after %d round(s)", repo, rounds)
            break

        response = _client.chat.completions.create(
            model=config.OPENAI_CHAT_MODEL,
            max_tokens=1024,
            messages=messages,
            tools=_TOOLS,
            tool_choice="auto",
        )
        if response.usage:
            total_prompt_tokens += response.usage.prompt_tokens
            total_completion_tokens += response.usage.completion_tokens
            log.info("github_agent: repo=%s round=%d tokens: prompt=%d completion=%d total=%d",
                      repo, rounds + 1, response.usage.prompt_tokens,
                      response.usage.completion_tokens, response.usage.total_tokens)

        choice = response.choices[0]
        tool_calls = choice.message.tool_calls
        if not tool_calls:
            break

        rounds += 1
        log.info("github_agent: repo=%s round=%d tool_calls=%s",
                  repo, rounds, [tc.function.name for tc in tool_calls])
        messages.append(choice.message)

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if len(files) >= config.MAX_FILES_PER_ERROR:
                result = {"error": f"file limit reached ({len(files)}/{config.MAX_FILES_PER_ERROR}) — finish diagnosing with what you have"}
            else:
                fn = dispatch.get(name)
                if fn is None:
                    result = {"error": f"unknown tool {name!r}"}
                else:
                    try:
                        result = fn(**args)
                    except Exception as e:
                        log.warning("github_agent: repo=%s tool=%s args=%s failed: %s", repo, name, args, e)
                        result = {"error": str(e)}

                    if name == "get_file_contents" and result.get("content") and result["path"] not in seen_paths:
                        files.append({"source_file": result["path"], "code": result["content"]})
                        seen_paths.add(result["path"])

            log.info("github_agent: repo=%s tool=%s args=%s -> %s",
                      repo, name, args, "ok" if "error" not in result else result["error"])
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result)[:config.MAX_FILE_CHARS],
            })

    if not primary_source_file and files:
        primary_source_file = files[0]["source_file"]

    log.info("github_agent: repo=%s done rounds=%d files=%s tokens: prompt=%d completion=%d total=%d",
              repo, rounds, [f["source_file"] for f in files],
              total_prompt_tokens, total_completion_tokens, total_prompt_tokens + total_completion_tokens)
    return {
        "files": files,
        "primary_source_file": primary_source_file,
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
    }
