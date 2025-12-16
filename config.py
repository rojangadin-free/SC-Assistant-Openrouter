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
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # Ensure this is set in .env

# Flask Configuration
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "your_default_secret_key")

# Vector Store Configuration
INDEX_NAME = "rag-google-v5"

# LLM Configuration (Google Gemini Models)
CHAT_MODEL_NAME = "gemini-3-pro-preview"
SUMMARIZER_MODEL_NAME = "gemini-2.5-flash"
VISION_MODEL_NAME = "gemini-2.5-flash"

if not PINECONE_API_KEY:
    raise ValueError("PINECONE_API_KEY is not set. Please check your .env file.")
if not GOOGLE_API_KEY:
    print("⚠️ WARNING: GOOGLE_API_KEY is not set.")