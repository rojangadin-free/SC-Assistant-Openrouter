import os
from sentence_transformers import SentenceTransformer, CrossEncoder

def download_models():
    # Must match the model name used in src/helper.py
    model_name = "sentence-transformers/all-MiniLM-L6-v2"

    print(f"Downloading embedding model: {model_name}...")
    # This initializes the model, which triggers the download to the local cache
    SentenceTransformer(model_name)
    print("Download complete.")

    # Must match RERANKER_MODEL_NAME in rag/reranker.py.
    # Cross-encoder used to reorder hybrid-search candidates (~90 MB).
    reranker_name = os.getenv(
        "RERANKER_MODEL_NAME", "cross-encoder/ms-marco-MiniLM-L6-v2"
    )
    print(f"Downloading reranker model: {reranker_name}...")
    CrossEncoder(reranker_name, max_length=512, device="cpu")
    print("Download complete.")

if __name__ == "__main__":
    download_models()
