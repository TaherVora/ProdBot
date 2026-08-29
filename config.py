import logging
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val

DATABASE_URL = _require("DATABASE_URL")
OPENAI_API_KEY = _require("OPENAI_API_KEY")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1")
OPENAI_ADAPT_MODEL = os.getenv("OPENAI_ADAPT_MODEL", "gpt-4.1-mini")

GITHUB_TOKEN = _require("GITHUB_TOKEN")

GCP_PROJECT_ID = _require("GCP_PROJECT_ID")
CLOUD_RUN_SERVICE = os.getenv("CLOUD_RUN_SERVICE", "cricket-fever")
LOG_LOOKBACK_MINUTES = int(os.getenv("LOG_LOOKBACK_MINUTES", "15"))

SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.2"))

# Below this distance AND with a matching reported filename, an error is
# treated as an exact repeat (Tier 1). Must stay well under
# SIMILARITY_THRESHOLD, which is now the *outer* bound for Tier 2 ("adapted").
EXACT_MATCH_THRESHOLD = float(os.getenv("EXACT_MATCH_THRESHOLD", "0.02"))

# Hard caps so a search_code tool call can never balloon into scanning the repo.
MAX_SEARCH_RESULTS = 10
MAX_FILE_CHARS = 6000  # per file, truncate large files before sending to the model

# Bounds for the agentic GitHub tool-calling loop (integrations/github_agent.py).
MAX_TOOL_CALL_ROUNDS = int(os.getenv("MAX_TOOL_CALL_ROUNDS", "4"))
MAX_FILES_PER_ERROR = int(os.getenv("MAX_FILES_PER_ERROR", "6"))
GITHUB_AGENT_TIMEOUT_SECONDS = int(os.getenv("GITHUB_AGENT_TIMEOUT_SECONDS", "60"))

# When no filename resolves, how many repo file paths to show the model up
# front (cheap — one deterministic Trees API call, no LLM round trip) so it
# picks from real filenames instead of guessing.
MAX_TREE_FILES_IN_PROMPT = int(os.getenv("MAX_TREE_FILES_IN_PROMPT", "300"))

# Build-tool/wrapper/doc noise stripped from the repo tree before it ever
# reaches a prompt — these could never plausibly be the file a fix needs.
TREE_EXCLUDE_PREFIXES = (
    ".git/", ".mvn/", ".idea/", ".vscode/", "target/", "build/", "node_modules/", "dist/",
)
TREE_EXCLUDE_FILENAMES = {
    "mvnw", "mvnw.cmd", "gradlew", "gradlew.bat", ".gitignore", ".gitattributes",
    "HELP.md", "LICENSE",
}
