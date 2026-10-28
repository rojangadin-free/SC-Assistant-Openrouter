"""Readiness checks configuration/shared storage, never paid model inference."""

import os
import threading
import time

_lock = threading.Lock()
_checked = 0.0
_ok = False


def probe():
    global _checked, _ok
    if os.getenv("SC_PRODUCTION", "false").lower() != "true":
        return True
    with _lock:
        if time.monotonic() - _checked < (15 if _ok else 3):
            return _ok
        try:
            from config import FLASK_SECRET_KEY, OPENROUTER_API_KEY, PINECONE_API_KEY
            if not OPENROUTER_API_KEY or not PINECONE_API_KEY:
                raise ValueError("Missing model/retrieval credentials")
            if not FLASK_SECRET_KEY or FLASK_SECRET_KEY == "your_default_secret_key":
                raise ValueError("Configure a non-default session signing secret")
            if os.getenv("STORE_BACKEND", "file").lower() != "dynamodb":
                raise ValueError("Production requires shared storage")
            from rag.artifacts import _manifest
            _manifest().mutate(lambda _: False)
            _ok = True
        except Exception as exc:
            print(f"[readiness] unavailable: {type(exc).__name__}")
            _ok = False
        _checked = time.monotonic()
        return _ok
