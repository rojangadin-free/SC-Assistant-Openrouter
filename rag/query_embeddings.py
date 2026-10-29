"""Bound local model inference and reuse query vectors, never student answers."""

import hashlib
import threading
from collections import OrderedDict

from langchain_core.embeddings import Embeddings

from rag.runtime import CapacityError, env_int


class CachedQueryEmbeddings(Embeddings):
    def __init__(self, model, cache_size=None):
        self.model = model
        self.cache_size = cache_size if cache_size is not None else env_int(
            "QUERY_EMBEDDING_CACHE_SIZE", 512, minimum=0,
        )
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._compute = threading.Lock()

    def embed_query(self, text):
        # Hashes keep the student's literal text out of cache keys. A model is
        # fixed for this object's lifetime, so vectors need no corpus invalidation.
        key = hashlib.sha256(text.encode("utf-8")).digest()
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return list(self._cache[key])
        if not self._compute.acquire(timeout=20):
            raise CapacityError("Query embedding is busy; please retry shortly.")
        try:
            with self._cache_lock:
                if key in self._cache:
                    self._cache.move_to_end(key)
                    return list(self._cache[key])
            vector = tuple(self.model.embed_query(text))
            if self.cache_size:
                with self._cache_lock:
                    self._cache[key] = vector
                    while len(self._cache) > self.cache_size:
                        self._cache.popitem(last=False)
            # Retrievers may scale a vector in-place. Never expose cached storage.
            return list(vector)
        finally:
            self._compute.release()

    def embed_documents(self, texts):
        if not self._compute.acquire(timeout=20):
            raise CapacityError("Document embedding is busy; please retry shortly.")
        try:
            return self.model.embed_documents(texts)
        finally:
            self._compute.release()
