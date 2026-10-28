"""Finite Pinecone socket waits and optional write-lease checks."""

from rag.runtime import env_float


class TimedIndex:
    def __init__(self, index, before_write=None):
        self.index = index
        self.before_write = before_write

    def query(self, *args, **kwargs):
        kwargs.setdefault("_request_timeout", env_float("PINECONE_REQUEST_TIMEOUT", 20, 1))
        return self.index.query(*args, **kwargs)

    def upsert(self, *args, **kwargs):
        if self.before_write:
            self.before_write()
        kwargs.setdefault("_request_timeout", env_float("PINECONE_REQUEST_TIMEOUT", 20, 1))
        return self.index.upsert(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.before_write:
            self.before_write()
        kwargs.setdefault("_request_timeout", env_float("PINECONE_REQUEST_TIMEOUT", 20, 1))
        return self.index.delete(*args, **kwargs)
