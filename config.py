import os
from dotenv import load_dotenv

# Load .env file from the project root
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

# AWS Configuration
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET_NAME = "sc-assistant"

# API Keys
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Flask Configuration
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "your_default_secret_key")

# Vector Store Configuration
INDEX_NAME = "rag-database3"

# LLM Configuration
CHAT_MODEL_NAME = "deepseek/deepseek-r1-0528:free"
SUMMARIZER_MODEL_NAME = "meta-llama/llama-3.3-8b-instruct:free"

# Add a check for the Pinecone API key to fail early if it's not set
if not PINECONE_API_KEY:
    raise ValueError("PINECONE_API_KEY is not set. Please check your .env file.")