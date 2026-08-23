"""
Embeddings for the dedup similarity check.

Since the POC will see "all kinds of errors" (not just the 503 timeout case),
we embed a normalized text representation of each error rather than the raw
log line, so two errors with the same shape but different timestamps/request
IDs still land close together in vector space.
"""

from integrations.llm_client import client as _client
import config


def normalize_error_text(error_type: str, endpoint: str, message: str) -> str:
    """
    Strips the volatile parts (timestamps, request IDs, specific values) down
    to the shape of the error, so 'timeout after 3001ms' and 'timeout after
    2894ms' on the same endpoint embed as near-duplicates instead of distinct
    errors.
    """
    parts = [p for p in [error_type, endpoint, message] if p]
    return " | ".join(parts)


def get_embedding(text: str) -> tuple[list[float], int]:
    """Returns (embedding, tokens_used) — the token count lets callers roll
    embedding cost into a per-call total alongside the chat-completion costs
    in integrations/github_agent.py and agent.py."""
    result = _client.embeddings.create(
        input=[text],
        model=config.OPENAI_EMBEDDING_MODEL,
    )
    tokens_used = result.usage.total_tokens if result.usage else 0
    return result.data[0].embedding, tokens_used
