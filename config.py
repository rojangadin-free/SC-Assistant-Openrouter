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
INDEX_NAME = "rag-google-v1"

# Express Mode API Key
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Flask Configuration
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "your_default_secret_key")

# LLM Configuration
CHAT_MODEL_NAME = "nvidia/nemotron-3-ultra-550b-a55b:free" 
FALLBACK_MODEL_NAME = "nvidia/nemotron-3-super-120b-a12b:free"
SUMMARIZER_MODEL_NAME = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

if not PINECONE_API_KEY:
    raise ValueError("PINECONE_API_KEY is not set. Please check your .env file.")