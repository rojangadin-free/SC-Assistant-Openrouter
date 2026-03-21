import os
from sentence_transformers import SentenceTransformer

def download_models():
    # Must match the model name used in src/helper.py
    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    
    print(f"Downloading embedding model: {model_name}...")
    # This initializes the model, which triggers the download to the local cache
    SentenceTransformer(model_name)
    print("Download complete.")

if __name__ == "__main__":
    download_models()