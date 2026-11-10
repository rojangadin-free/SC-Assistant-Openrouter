"""Short-lived request coordination for Cognito's standard password recovery.

Only an opaque ID goes in the Flask cookie. Passwords, entered codes, and provider
tokens are never stored here. Cognito validates the code and changes the password
in one operation; local state binds the target and limits retries across workers.
"""

import hashlib
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from contextlib import closing, contextmanager

from rag.store import _file_transaction


class ResetFlowError(Exception):
    def __init__(self, message, status=400, retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class ResetFlowStore:
    def __init__(self, path=None, clock=None):
        pool = hashlib.sha256(os.getenv("COGNITO_USER_POOL_ID", "default").encode()).hexdigest()[:12]
        self.path = path or os.getenv("PASSWORD_RESET_STATE_PATH") or os.path.join(
            tempfile.gettempdir(), f"sc-auth-{pool}", "password-reset.sqlite3",
        )
        self.clock = clock or time.time
        self._initialized = False
        self._lock = threading.Lock()

    def _connect(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), mode=0o700, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        conn = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                if not self._initialized:
                    with _file_transaction(self.path):
                        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                            conn.execute("PRAGMA journal_mode=WAL")
                        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        if not {"reset_flows", "reset_limits"}.issubset(tables):
                            conn.executescript("""
                                CREATE TABLE IF NOT EXISTS reset_flows (
                                    id TEXT PRIMARY KEY, email TEXT NOT NULL,
                                    username TEXT, subject TEXT, challenge TEXT,
                                    stage TEXT NOT NULL, attempts INTEGER NOT NULL,
                                    expires REAL NOT NULL, resend_at REAL NOT NULL
                                );
                                CREATE TABLE IF NOT EXISTS reset_limits (
                                    key TEXT PRIMARY KEY, next_send REAL NOT NULL
                                );
                            """)
                    self._initialized = True
            return conn
        except BaseException:
            conn.close()
            raise

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def begin(self, email, previous_id=None):
        now = self.clock()
        flow_id = secrets.token_urlsafe(32)
        key = hashlib.sha256(email.encode()).hexdigest()
        with self._transaction() as conn:
            conn.execute("DELETE FROM reset_flows WHERE expires <= ?", (now,))
            conn.execute("DELETE FROM reset_limits WHERE next_send <= ?", (now,))
            limit = conn.execute("SELECT next_send FROM reset_limits WHERE key = ?", (key,)).fetchone()
            if limit:
                raise ResetFlowError("Please wait before requesting another reset code.", 429,
                                     max(1, int(limit[0] - now + 0.999)))
            if conn.execute("SELECT COUNT(*) FROM reset_flows").fetchone()[0] >= 100:
                raise ResetFlowError("Password recovery is busy. Please try again shortly.", 429, 30)
            # A new request invalidates older local reset contexts, including another
            # browser's reset for the same address and this browser's old target.
            conn.execute("DELETE FROM reset_flows WHERE email = ? OR id = ?", (email, previous_id))
            conn.execute("INSERT INTO reset_flows VALUES (?, ?, NULL, NULL, NULL, 'sending', 0, ?, ?)",
                         (flow_id, email, now + 180, now + 30))
            conn.execute("INSERT OR REPLACE INTO reset_limits VALUES (?, ?)", (key, now + 30))
        return flow_id

    def mark_code_sent(self, flow_id, username):
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE reset_flows SET username=?, challenge=NULL, subject=NULL, stage='reset' "
                "WHERE id=? AND stage='sending' AND expires>?",
                (username, flow_id, self.clock()),
            ).rowcount
            if not changed:
                raise ResetFlowError("This reset request was replaced or expired. Request a new code.")

    def get(self, flow_id):
        if not flow_id:
            return None
        with self._transaction() as conn:
            conn.execute("DELETE FROM reset_flows WHERE expires <= ?", (self.clock(),))
            row = conn.execute("SELECT * FROM reset_flows WHERE id=?", (flow_id,)).fetchone()
            return dict(row) if row else None

    def public_state(self, flow_id):
        row = self.get(flow_id)
        if not row or row["stage"] not in ("sending", "reset", "writing"):
            return {"stage": "expired", "email": "", "resend_available_at": 0,
                    "server_now": self.clock() * 1000}
        return {"stage": "code" if row["stage"] == "reset" else row["stage"],
                "email": row["email"], "expires_at": row["expires"] * 1000,
                "resend_available_at": row["resend_at"] * 1000, "server_now": self.clock() * 1000}

    def claim_password_change(self, flow_id, *, email=None):
        """Claim one attempt; this does not bypass Cognito's code verification."""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM reset_flows WHERE id=? AND expires>?", (flow_id, self.clock())).fetchone()
            if not row or row["stage"] not in ("reset", "writing"):
                raise ResetFlowError("Request a new reset code before changing your password.")
            if row["attempts"] >= 5:
                raise ResetFlowError("The reset code has too many attempts. Request a new code.")
            if row["stage"] == "writing":
                raise ResetFlowError("This reset request is being processed. Please wait.", 409)
            if email is not None and email != row["email"]:
                raise ResetFlowError("Email changed. Send a new OTP for the address shown before resetting your password.")
            conn.execute("UPDATE reset_flows SET stage='writing', attempts=attempts+1 WHERE id=?", (flow_id,))
            return dict(row)

    def finish_password_change(self, flow_id, retry=False):
        with self._transaction() as conn:
            if retry:
                conn.execute("UPDATE reset_flows SET stage=CASE WHEN attempts<5 THEN 'reset' ELSE 'failed' END "
                             "WHERE id=? AND stage='writing' AND expires>?",
                             (flow_id, self.clock()))
            else:
                conn.execute("DELETE FROM reset_flows WHERE id=?", (flow_id,))

    def discard(self, flow_id):
        with self._transaction() as conn:
            conn.execute("DELETE FROM reset_flows WHERE id=?", (flow_id,))


reset_flows = ResetFlowStore()
