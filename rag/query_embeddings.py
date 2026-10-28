"""Cache query vectors and serialize local embedding inference, not answers."""

import threading

from langchain_core.embeddings import Embeddings
from rag.runtime import TTLCache, env_int, env_float


class CachedQueryEmbeddings(Embeddings):
    def __init__(self, model):
        self.model = model
        self._inference = threading.Lock()
        self._cache = TTLCache(
            env_int("EMBEDDING_CACHE_ENTRIES", 256, 0),
            env_float("EMBEDDING_CACHE_TTL", 300),
        )

    def embed_query(self, text):
        def compute():
            with self._inference:
                return self.model.embed_query(text)
        return self._cache.get_or_compute(text, compute)

    def embed_documents(self, texts):
        with self._inference:
            return self.model.embed_documents(texts)
