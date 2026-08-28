from __future__ import annotations

import json
import logging
import time

import config
from integrations import github_agent
from integrations.llm_client import client as _client

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a solution architect diagnosing production errors for a backend \
service. You have read-only tools to fetch files, list directories, find files by name, and \
search code in one GitHub repository — use them to gather whatever code you need before \
answering; you are not limited to a single file if the fix may span more than one (e.g. a \
repository method signature, a DTO field, an exception handler).

Every round of tool calls resends the full conversation so far, so fetching files one at a \
time across many rounds is significantly more expensive than fetching them together. If you \
can already predict you'll need several files — e.g. a controller plus the exception/model/ \
repository classes it obviously depends on, once you've seen imports or method calls naming \
them — request all of them as multiple get_file_contents calls in the SAME round instead of \
fetching one, waiting to see it, then fetching the next.

search_code hits GitHub's code-search API, which has a much lower rate limit than \
list_directory/find_file_by_name/get_file_contents (those use a separate, far more generous \
API). Prefer list_directory or find_file_by_name whenever you have any name/path hint at all; \
use search_code only when there's nothing else to try, and never retry it more than once per \
diagnosis — if it fails, switch tools instead of trying a different search term.

Common causes worth checking for timeout/503 errors on data fetching: upstream service \
latency, missing or misconfigured retry/backoff, connection pool exhaustion, and \
too-short client timeouts relative to the upstream's actual response time.

Once you've gathered enough context, or your tools aren't turning up anything more useful, \
stop calling tools and write your full diagnosis directly as your reply — that reply is the \
final answer, not a summary. Rules for that final answer:
- If source code is available, ground your fix in it: name the specific function and line \
  number to change, and start that file's answer with "Line <N>:" citing the exact line (file \
  contents you fetch are shown with line numbers for this purpose).
- Before you conclude, explicitly check EVERY file you fetched, not just the first one where \
  you spotted a problem — a real fix commonly spans more than one (e.g. a controller returning \
  the wrong status, an exception class missing a mapping, a repository method's signature, a \
  DTO field). Do not stop as soon as you have one plausible fix; go through the rest and only \
  skip a file once you're confident it needs no change. For every file that DOES need a change, \
  give it its own clearly labeled section with a "Line <N>:" citation — do not silently merge \
  multiple files' fixes into one answer or only mention the single most obvious one.
- If no source code is available (or nothing you found is actually relevant), say so \
  explicitly and give a general, pattern-based recommendation rather than inventing specifics \
  about code you haven't seen.
- Concise per file: a short diagnosis then a concrete fix, no preamble — "concise" means no \
  fluff per section, not fewer files covered.
"""


def diagnose(
    raw_log: str, filename: str | None, repo: str = None, path_prefix: str = None,
    branch: str = None, service_name: str | None = None, reported_line: int | None = None,
) -> dict:
    """
    Returns {"solution": str, "files": [{"source_file": str, "code": str}, ...],
              "primary_source_file": str|None, "prompt_tokens": int, "completion_tokens": int}

    One continuous conversation: gather code via tool calls (integrations/github_agent.py's
    tool schemas/dispatch), then the model's own final (non-tool-calling) reply IS the
    diagnosis — no second, separate call that would resend already-fetched file content
    from scratch.
    """
    total_prompt_tokens = 0
    total_completion_tokens = 0

    context_header = f"Service: {service_name or 'unknown'}\nError log:\n{raw_log}"
    if reported_line:
        context_header += f"\nReported at line: {reported_line}"

    if not repo:
        log.info("diagnose: no repo mapping -> no context, single no-tools call")
        response = _client.chat.completions.create(
            model=config.OPENAI_CHAT_MODEL,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"{context_header}\n\nNo source code could be located "
                                             f"for this error (no repo mapping for this service)."},
            ],
        )
        solution = response.choices[0].message.content
        if response.usage:
            total_prompt_tokens = response.usage.prompt_tokens
            total_completion_tokens = response.usage.completion_tokens
            log.info("diagnose: service=%r tokens: prompt=%d completion=%d total=%d",
                      service_name, total_prompt_tokens, total_completion_tokens, response.usage.total_tokens)
        return {
            "solution": solution, "files": [], "primary_source_file": None,
            "prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens,
        }

    branch = branch or "main"

    seed = github_agent.resolve_seed(filename, repo, path_prefix, branch)
    files: list[dict] = []
    seen_paths: set[str] = set()
    primary_source_file = None
    if seed:
        files.append(seed)
        seen_paths.add(seed["source_file"])
        primary_source_file = seed["source_file"]

    tree = github_agent.fetch_filtered_tree(repo, branch, path_prefix)
    state = {"search_disabled": False}
    dispatch = github_agent._make_dispatch(repo, branch, path_prefix, state, tree_cache=tree)

    tree_listing = "\n".join(tree[: config.MAX_TREE_FILES_IN_PROMPT])
    truncation_note = ""
    if len(tree) > config.MAX_TREE_FILES_IN_PROMPT:
        truncation_note = f"\n... ({len(tree) - config.MAX_TREE_FILES_IN_PROMPT} more file(s) not shown)"

    seed_text = ""
    if files:
        seed_text = "\n\n".join(
            f"Already fetched via filename match ({f['source_file']}), line-numbered:\n{f['code']}" for f in files
        )
        tree_message = (
            f"{context_header}\n\n{seed_text}\n\nRepo file listing — if the file(s) above don't "
            f"fully explain or fix this error, pick any other file(s) you need straight from here "
            f"with get_file_contents using the exact path shown, rather than exploring blind:\n"
            f"{tree_listing}{truncation_note}"
        )
    else:
        # No filename resolved — don't make the model guess filenames blind (it
        # has no idea what language/framework this repo even uses).
        tree_message = (
            f"{context_header}\n\nNo file could be resolved automatically from the filename. Here "
            f"is the repo's file listing — pick the file(s) most likely relevant to this error "
            f"(e.g. a controller/handler matching the route in the log) and fetch them with "
            f"get_file_contents using the exact path shown below, rather than guessing filenames:\n"
            f"{tree_listing}{truncation_note}"
        )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": tree_message},
    ]
    tree_message_index = 1
    tree_shrunk = False

    start = time.monotonic()
    rounds = 0
    solution: str | None = None

    while rounds < config.MAX_TOOL_CALL_ROUNDS:
        if time.monotonic() - start > config.GITHUB_AGENT_TIMEOUT_SECONDS:
            log.warning("diagnose: repo=%s timed out after %d round(s)", repo, rounds)
            break

        response = _client.chat.completions.create(
            model=config.OPENAI_CHAT_MODEL,
            max_tokens=1024,
            messages=messages,
            tools=github_agent._TOOLS,
            tool_choice="auto",
        )
        if response.usage:
            total_prompt_tokens += response.usage.prompt_tokens
            total_completion_tokens += response.usage.completion_tokens
            log.info("diagnose: repo=%s round=%d tokens: prompt=%d completion=%d total=%d",
                      repo, rounds + 1, response.usage.prompt_tokens,
                      response.usage.completion_tokens, response.usage.total_tokens)

        choice = response.choices[0]
        tool_calls = choice.message.tool_calls
        if not tool_calls:
            solution = choice.message.content
            break

        rounds += 1
        log.info("diagnose: repo=%s round=%d tool_calls=%s",
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
                        log.warning("diagnose: repo=%s tool=%s args=%s failed: %s", repo, name, args, e)
                        result = {"error": str(e)}

                    if name == "get_file_contents" and result.get("content") and result["path"] not in seen_paths:
                        files.append({"source_file": result["path"], "code": result["content"]})
                        seen_paths.add(result["path"])

            log.info("diagnose: repo=%s tool=%s args=%s -> %s",
                      repo, name, args, "ok" if "error" not in result else result["error"])
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result)[:config.MAX_FILE_CHARS],
            })

        # Once the model has made its first round of picks, the full tree listing
        # is no longer needed in every subsequent round's resent history — replace
        # it with a short pointer so we stop paying for it repeatedly. Keep the
        # seed file's content (if any) intact — that's still-relevant code the
        # model needs for the rest of the conversation, not exploration scaffolding.
        if not tree_shrunk:
            shrunk_content = context_header
            if seed_text:
                shrunk_content += f"\n\n{seed_text}"
            shrunk_content += (
                "\n\n(repo file tree was shown here initially; call list_directory if you "
                "need to browse it again — it's cached, no extra cost)"
            )
            messages[tree_message_index] = {"role": "user", "content": shrunk_content}
            tree_shrunk = True

    if solution is None:
        # Bounds hit mid-loop with tool calls still pending, or the loop never got
        # a natural stop — force one last answer reusing existing history (still
        # cheaper than a from-scratch call: no already-fetched file content is
        # resent, just one short instruction message).
        messages.append({
            "role": "user",
            "content": "Stop exploring — provide your diagnosis now based on everything gathered so far.",
        })
        response = _client.chat.completions.create(
            model=config.OPENAI_CHAT_MODEL,
            max_tokens=1024,
            messages=messages,
        )
        solution = response.choices[0].message.content
        if response.usage:
            total_prompt_tokens += response.usage.prompt_tokens
            total_completion_tokens += response.usage.completion_tokens
            log.info("diagnose: repo=%s forced final call tokens: prompt=%d completion=%d total=%d",
                      repo, response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens)

    if not primary_source_file and files:
        primary_source_file = files[0]["source_file"]

    if files:
        files_considered = ", ".join(f"`{f['source_file']}`" for f in files)
        solution = f"**Files considered:** {files_considered}\n\n{solution}"

    log.info("diagnose: repo=%s done rounds=%d files=%s tokens: prompt=%d completion=%d total=%d",
              repo, rounds, [f["source_file"] for f in files],
              total_prompt_tokens, total_completion_tokens, total_prompt_tokens + total_completion_tokens)

    return {
        "solution": solution,
        "files": files,
        "primary_source_file": primary_source_file,
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
    }
