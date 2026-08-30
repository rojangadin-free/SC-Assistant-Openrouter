import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

# AWS Configuration
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")
COGNITO_CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET_NAME = "sc-assistant-bucket"

# Vector Store Configuration
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
INDEX_NAME = "rag-database-2026"

# Express Mode API Key
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
AGENTROUTER_API_KEY = os.getenv("AGENTROUTER_API_KEY")

# Flask Configuration
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "your_default_secret_key")

# LLM Configuration
CHAT_MODEL_NAME = "deepseek-v4-flash"

# The fallback must be reachable through a DIFFERENT provider than the primary.
# AgentRouter fronts requests with a content filter that rejects some perfectly
# ordinary campus questions ("latin honor" -> HTTP 400 content-blocked). Pointing
# the fallback at the same gateway means the retry is filtered by the same rule
# that rejected the first attempt, so the student sees "Streaming interrupted."
# with no answer at all. This model is served by OpenRouter, so a provider-side
# block on one is not a block on the other.
FALLBACK_MODEL_NAME = "nvidia/nemotron-3-ultra-550b-a55b:free"

SUMMARIZER_MODEL_NAME = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"


if not PINECONE_API_KEY:
    raise ValueError("PINECONE_API_KEY is not set. Please check your .env file.")