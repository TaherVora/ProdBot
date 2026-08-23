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
    raw_log: str, code: str | None, source_file: str | None,
    related_files: list[dict] | None = None, service_name: str | None = None,
    reported_line: int | None = None,
) -> str:
    related_files = related_files or []

    parts = [f"Service: {service_name or 'unknown'}", f"Error log:\n{raw_log}"]
    if reported_line:
        parts.append(f"Reported at line: {reported_line}")
    if code:
        parts.append(f"Primary file ({source_file}), line-numbered:\n{_line_numbered(code)}")
        for rel in related_files:
            parts.append(
                f"Related file ({rel['source_file']}), line-numbered:\n{_line_numbered(rel['code'])}"
            )
    else:
        parts.append("No source code could be located for this error.")

    log.info(
        "generate_solution: service=%r source_file=%r code_provided=%s related_files=%s",
        service_name, source_file, code is not None, [r["source_file"] for r in related_files],
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

    if source_file:
        files_considered = ", ".join(f"`{f}`" for f in [source_file] + [r["source_file"] for r in related_files])
        solution = f"**Files considered:** {files_considered}\n\n{solution}"
    return solution
