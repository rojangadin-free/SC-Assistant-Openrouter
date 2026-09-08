import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

# AWS Configuration
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")
COGNITO_CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")
S3_BUCKET_NAME = "sc-assistant-bucket2"

# Vector Store Configuration
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
INDEX_NAME = "rag-database-2026"

# Express Mode API Key
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
AGENTROUTER_API_KEY = os.getenv("AGENTROUTER_API_KEY")

# Flask Configuration
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "your_default_secret_key")

# Where admin state lives (see docs/SHARED_STORAGE.md and rag/store.py).
#
# `rag/store.py` reads STORE_BACKEND from the environment, and `.env` is not in
# the image — it is gitignored, and the deploy passes secrets in as individual
# `-e` flags. So the container fell back to the `file` default and wrote every
# admin decision to its own writable layer, while the developer's machine (which
# does have .env) wrote to DynamoDB. The two were never looking at the same data:
# a calendar period saved locally simply did not exist for the deployed app, and
# anything the deployed app saved was discarded on the next redeploy.
#
# Defaulting to `dynamodb` here, rather than adding another `-e` line to the
# workflow, is deliberate: the failure mode of a MISSING env var is silent
# divergence that looks like a UI bug, and the next store added would inherit the
# same trap. `file` is still selectable for offline work — `run_tests.py` sets it
# explicitly — but the shared backend is now what you get by not thinking about it.
STORE_BACKEND = os.getenv("STORE_BACKEND", "dynamodb")
os.environ["STORE_BACKEND"] = STORE_BACKEND
STORE_TABLE_NAME = os.getenv("STORE_TABLE_NAME", "SCAssistantStores")
os.environ["STORE_TABLE_NAME"] = STORE_TABLE_NAME


# LLM Configuration
CHAT_MODEL_NAME = "deepseek/deepseek-v4-flash-0731"

# The fallback must be reachable through a DIFFERENT provider than the primary.
# AgentRouter fronts requests with a content filter that rejects some perfectly
# ordinary campus questions ("latin honor" -> HTTP 400 content-blocked). Pointing
# the fallback at the same gateway means the retry is filtered by the same rule
# that rejected the first attempt, so the student sees "Streaming interrupted."
# with no answer at all. This model is served by OpenRouter, so a provider-side
# block on one is not a block on the other.
FALLBACK_MODEL_NAME = "deepseek/deepseek-v4-flash-vision-exp"

SUMMARIZER_MODEL_NAME = "google/gemini-2.5-flash-lite"


if not PINECONE_API_KEY:
    raise ValueError("PINECONE_API_KEY is not set. Please check your .env file.")