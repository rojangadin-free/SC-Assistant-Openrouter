"""Single-container, cross-worker chat admission and temporary guest history.

SQLite is shared by Gunicorn workers on this machine, not by different servers.
Authenticated history and durable admin state remain in DynamoDB.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import closing

from rag.runtime import CapacityError, env_int
from rag.store import _file_transaction


class ConcurrentTurnError(CapacityError):
    pass


class ChatRuntime:
    def __init__(self, path=None):
        workspace = hashlib.sha256(os.path.dirname(__file__).encode()).hexdigest()[:12]
        self.path = path or os.getenv("SC_RUNTIME_DB") or os.path.join(
            tempfile.gettempdir(), f"sc-chat-{workspace}.sqlite3",
        )
        self._initialized = False
        self._init_lock = threading.Lock()

    def _connect(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        try:
            with self._init_lock:
                if not self._initialized:
                    # journal_mode changes can fail immediately during concurrent
                    # startup, even with busy_timeout. Serialize bootstrap across
                    # workers and avoid rewriting an already initialized schema.
                    with _file_transaction(self.path):
                        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                            conn.execute("PRAGMA journal_mode=WAL")
                        tables = {row[0] for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'",
                        )}
                        if not {"chat_leases", "guest_history"}.issubset(tables):
                            conn.executescript("""
                                CREATE TABLE IF NOT EXISTS chat_leases (
                                    token TEXT PRIMARY KEY, session_key TEXT UNIQUE NOT NULL,
                                    image INTEGER NOT NULL, expires REAL NOT NULL
                                );
                                CREATE TABLE IF NOT EXISTS guest_history (
                                    session_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                                    expires REAL NOT NULL, updated REAL NOT NULL
                                );
                            """)
                    self._initialized = True
            return conn
        except BaseException:
            conn.close()
            raise

    def acquire(self, session_key, *, image=False, seconds=120):
        token = uuid.uuid4().hex
        key = hashlib.sha256(session_key.encode()).hexdigest()
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM chat_leases WHERE expires <= ?", (now,))
                if conn.execute("SELECT 1 FROM chat_leases WHERE session_key = ?", (key,)).fetchone():
                    raise ConcurrentTurnError("An answer is already running in this conversation.")
                count, images = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(image), 0) FROM chat_leases",
                ).fetchone()
                if count >= env_int("CHAT_MAX_ACTIVE", 30):
                    raise CapacityError("The assistant is busy. Please retry in a few seconds.")
                if image and images >= env_int("CHAT_MAX_IMAGES", 2):
                    raise CapacityError("Image processing is busy. Please retry shortly.")
                conn.execute("INSERT INTO chat_leases VALUES (?, ?, ?, ?)",
                             (token, key, int(image), now + seconds))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return token

    def release(self, token):
        if token:
            with closing(self._connect()) as conn:
                conn.execute("DELETE FROM chat_leases WHERE token = ?", (token,))

    def load_guest(self, session_key):
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload FROM guest_history WHERE session_key = ? AND expires > ?",
                (session_key, time.time()),
            ).fetchone()
        if not row:
            return []
        history = json.loads(row[0])
        if not isinstance(history, list):
            raise TypeError("Invalid temporary conversation history.")
        return history

    def save_guest(self, session_key, history):
        # Bound both age and storage. Keep complete recent turns, not image data.
        history = list(history[-20:])
        payload = json.dumps(history, ensure_ascii=False)
        while len(payload.encode("utf-8")) > 256 * 1024 and len(history) > 2:
            history = history[2:]
            payload = json.dumps(history, ensure_ascii=False)
        if len(payload.encode("utf-8")) > 256 * 1024:
            raise ValueError("Temporary conversation exceeds its storage budget.")
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM guest_history WHERE expires <= ?", (now,))
                conn.execute(
                    "INSERT INTO guest_history VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(session_key) DO UPDATE SET payload=excluded.payload, "
                    "expires=excluded.expires, updated=excluded.updated",
                    (session_key, payload, now + env_int("GUEST_HISTORY_TTL_SECONDS", 7200), now),
                )
                conn.execute(
                    "DELETE FROM guest_history WHERE session_key IN "
                    "(SELECT session_key FROM guest_history ORDER BY updated DESC LIMIT -1 OFFSET ?)",
                    (env_int("GUEST_HISTORY_MAX_SESSIONS", 300),),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def delete_guest(self, session_key):
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM guest_history WHERE session_key = ?", (session_key,))


chat_runtime = ChatRuntime()
