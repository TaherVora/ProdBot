"""Shared OpenAI client, used by both embeddings.py (dedup) and agent.py (solution generation)."""

import openai

import config

client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
