from __future__ import annotations

import logging

from integrations.llm_client import client as _client
import config

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a solution architect diagnosing production errors for a backend \
service. You'll be given the service name, an error log, optionally the file/line the app \
reported, and optionally the source code that file/line points to (possibly along with other \
same-project files it depends on, such as a repository, DTO, or another layer it calls into).

Common causes worth checking for timeout/503 errors on data fetching: upstream service \
latency, missing or misconfigured retry/backoff, connection pool exhaustion, and \
too-short client timeouts relative to the upstream's actual response time.

Rules:
- If source code is provided, ground your fix in it: name the specific function and line \
  number to change, and start that file's answer with "Line <N>:" citing the exact line.
- If additional related files are provided, check each one too — the fix may need changes \
  in more than just the primary file (e.g. a repository method signature, a DTO field). Give \
  a separate, clearly labeled solution per file that actually needs a change. Don't mention \
  a related file at all if it doesn't need changes.
- If no source code is provided, say so explicitly and give a general, pattern-based \
  recommendation rather than inventing specifics about code you haven't seen.
- Be concise: a short diagnosis, then a concrete fix. No preamble.
"""


def _line_numbered(code: str) -> str:
    return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(code.splitlines()))


def generate_solution(
    raw_log: str, files: list[dict] | None, primary_source_file: str | None = None,
    service_name: str | None = None, reported_line: int | None = None,
) -> dict:
    """
    Returns {"solution": str, "prompt_tokens": int, "completion_tokens": int}

    `files` is the flattened list the agentic retrieval loop assembled
    (integrations/github_agent.py's gather_code_context) — no more
    primary/related split, since the model decided its own file set.
    `primary_source_file`, if set, is ordered first for the "Line <N>:"
    citation instruction below.
    """
    files = files or []
    if primary_source_file:
        files = sorted(files, key=lambda f: f["source_file"] != primary_source_file)

    parts = [f"Service: {service_name or 'unknown'}", f"Error log:\n{raw_log}"]
    if reported_line:
        parts.append(f"Reported at line: {reported_line}")
    if files:
        for f in files:
            label = "Primary file" if f["source_file"] == primary_source_file else "Related file"
            parts.append(f"{label} ({f['source_file']}), line-numbered:\n{_line_numbered(f['code'])}")
    else:
        parts.append("No source code could be located for this error.")

    log.info(
        "generate_solution: service=%r primary_source_file=%r files=%s",
        service_name, primary_source_file, [f["source_file"] for f in files],
    )

    response = _client.chat.completions.create(
        model=config.OPENAI_CHAT_MODEL,
        max_tokens=1024,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
    )
    solution = response.choices[0].message.content
    prompt_tokens = response.usage.prompt_tokens if response.usage else 0
    completion_tokens = response.usage.completion_tokens if response.usage else 0

    if response.usage:
        log.info(
            "generate_solution: service=%r tokens: prompt=%d completion=%d total=%d",
            service_name, prompt_tokens, completion_tokens, response.usage.total_tokens,
        )

    if files:
        files_considered = ", ".join(f"`{f['source_file']}`" for f in files)
        solution = f"**Files considered:** {files_considered}\n\n{solution}"
    return {"solution": solution, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
