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

GITHUB_TOKEN = _require("GITHUB_TOKEN")

GCP_PROJECT_ID = _require("GCP_PROJECT_ID")
CLOUD_RUN_SERVICE = os.getenv("CLOUD_RUN_SERVICE", "voc-trending")
LOG_LOOKBACK_MINUTES = int(os.getenv("LOG_LOOKBACK_MINUTES", "15"))

SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.15"))

# Hard caps so a Tier 2 code search can never balloon into scanning the repo.
MAX_SEARCH_RESULTS = 10
MAX_FILE_CHARS = 6000  # per file, truncate large files before sending to the model

# Cap on how many same-project files (repository/DTO/etc. that the primary
# file imports) get pulled in as extra grounding for the fix.
MAX_RELATED_FILES = 3
