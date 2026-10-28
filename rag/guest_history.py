"""Expiring guest turns shared by all workers, separate from student records."""

import copy
import hashlib
import json
import os
import tempfile
import time

from rag.runtime import env_int
from rag.store import JsonBlobStore


class GuestHistory:
    def _store(self, session_id):
        if not session_id:
            raise ValueError("A guest session ID is required")
        key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        directory = os.getenv("GUEST_HISTORY_DIR") or os.path.join(
            tempfile.gettempdir(), "sc-assistant-guests"
        )
        if (os.getenv("STORE_BACKEND") or "file").lower() == "file":
            os.makedirs(directory, exist_ok=True)
        return JsonBlobStore("guest_history:" + key, os.path.join(directory, key + ".json"),
                             lambda: {"messages": []})

    def load(self, session_id):
        def expire(data):
            if data.get("messages") and data.get("expires_at", 0) <= time.time():
                data["messages"] = []
                return None
            return False
        # mutate(False) is a strict consistent read, unlike best-effort prompt stores.
        data = self._store(session_id).mutate(expire)
        return copy.deepcopy(data.get("messages") or [])

    def append(self, session_id, messages):
        def add(data):
            history = data.get("messages") or []
            if data.get("expires_at", 0) <= time.time():
                history = []
            history = history + copy.deepcopy(messages)
            maximum = env_int("GUEST_HISTORY_MAX_MESSAGES", 40, 2)
            if maximum % 2:
                maximum -= 1
            history = history[-maximum:]
            budget = env_int("GUEST_HISTORY_MAX_BYTES", 120 * 1024, 1024)
            while len(history) > 2 and len(json.dumps(history, ensure_ascii=False).encode("utf-8")) > budget:
                history = history[2:]
            if len(json.dumps(history, ensure_ascii=False).encode("utf-8")) > budget:
                raise ValueError("Guest conversation exceeds its retention budget")
            data["messages"] = history
            data["expires_at"] = int(time.time()) + env_int("GUEST_HISTORY_TTL_SECONDS", 3600)
        return self._store(session_id).mutate(add)["messages"]

    def clear(self, session_id):
        def remove(data):
            data["messages"] = []
            data["expires_at"] = int(time.time())
        self._store(session_id).mutate(remove)


guest_history = GuestHistory()
