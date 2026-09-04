"""
Pre-download the models into the local HuggingFace cache.

Run once on a new machine, and at image build time (see Dockerfile):

    python download_model.py

Why this file must agree with rag/reranker.py
---------------------------------------------
It didn't, and that is what "no score on all 12 documents" was. This script
fetched `cross-encoder/ms-marco-MiniLM-L6-v2` while `rag/reranker.py` loaded
`cross-encoder/ettin-reranker-32m-v1`, so the model baked into the image was
never the model the app asked for. Nothing errored: `_load_model()` catches the
failure, `rerank()` hands the documents back untouched, no document gets a
`rerank_score`, and `retrieve_documents()` prints `score=n/a` for every one of
them while answers quietly fall back to raw hybrid-retrieval order.

Both files now name the ms-marco model, and that choice is constrained, not
arbitrary: `requirements.txt` pins `sentence-transformers==3.3.1`, which holds
`transformers` on the 4.x line. Pointing this script at the ettin reranker failed
the image build outright —

    ValueError: Tokenizer class TokenizersBackend does not exist
                or is not currently imported.

— because that model's tokenizer_config names a class only the newer transformers
line provides. Upgrading the pins is the prerequisite for changing the model, in
that order.

The ids are duplicated as literals rather than imported, on purpose: the
Dockerfile copies THIS FILE ALONE and runs it before copying the application,
so the ~90 MB download is cached in its own layer and survives ordinary code
edits. Importing `rag.reranker` here would either break that build stage or
force the model layer to rebuild on every commit. tests/test_reranker_model.py
compares the literals instead, so the two can no longer drift silently.
"""

import os

from sentence_transformers import CrossEncoder, SentenceTransformer

# Must match EMBEDDING model used in src/helper.py
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Must match DEFAULT_RERANKER_MODEL in rag/reranker.py (guarded by a test).
DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"



def download_models():
    print(f"Downloading embedding model: {EMBEDDING_MODEL}...")
    # Instantiating the model triggers the download into the local cache.
    SentenceTransformer(EMBEDDING_MODEL)
    print("Download complete.")

    # Same env var the app reads, so overriding the model in a deployment also
    # pre-downloads the right one instead of leaving the app to fetch it during
    # a student's request.
    reranker_name = os.getenv("RERANKER_MODEL_NAME", DEFAULT_RERANKER_MODEL)
    print(f"Downloading reranker model: {reranker_name}...")
    CrossEncoder(reranker_name, device="cpu")
    print("Download complete.")


if __name__ == "__main__":
    download_models()
