"""Request-frequency limits shared by workers on one host/container.

Counters contain keyed identity digests, never request bodies or raw addresses.
Use a shared network-backed limiter before deploying independent app replicas.
"""

import hashlib
import hmac
import ipaddress
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from flask import jsonify, request, session
from werkzeug.middleware.proxy_fix import ProxyFix


DEFAULT_RULES = {
    "api": (300, 60),
    "write": (120, 60),
    "chat": (20, 60),
    "guest_chat": (6, 60),
    "login": (60, 60),
    "login_target": (10, 600),
    "signup": (30, 600),
    "code_send": (20, 600),
    "verify": (30, 60),
    "feedback": (60, 60),
    "report": (10, 600),
    "upload": (6, 300),
    "scan": (6, 300),
    "poll": (600, 60),
}

_WRITE_SCOPES = {
    "auth.login": "login",
    "auth.signup": "signup",
    "auth.handle_forgot_password": "code_send",
    "auth.resend_signup": "code_send",
    "auth.confirm_signup": "verify",
    "auth.handle_reset_password": "verify",
    "settings.change_password": "verify",
    "chat.submit_feedback": "feedback",
    "chat.submit_report": "report",
    "chat.escalate": "report",
    "admin.upload_file": "upload",
    "freshness.api_scan_upload": "scan",
    "chat.read_feedback_batch": "api",
}


class SQLiteRateStore:
    """First-request-anchored fixed windows, with atomic cross-worker admission."""

    def __init__(self, path, clock=None):
        self.path = os.fspath(path)
        self.clock = clock or time.time
        self._initialized = False

    def consume(self, key, limit, window):
        if type(limit) is not int or type(window) is not int or limit < 1 or window < 1:
            raise ValueError("Rate limits require positive integer counts and windows.")
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=3, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not self._initialized:
                    conn.execute("CREATE TABLE IF NOT EXISTS request_limits "
                                 "(key TEXT PRIMARY KEY, hits INTEGER NOT NULL, reset_at REAL NOT NULL)")
                    conn.execute("CREATE INDEX IF NOT EXISTS request_limits_expiry ON request_limits(reset_at)")
                now = self.clock()
                conn.execute("DELETE FROM request_limits WHERE reset_at <= ?", (now,))
                row = conn.execute("SELECT hits, reset_at FROM request_limits WHERE key = ?", (key,)).fetchone()
                retry_after = 0
                if row and row[0] >= limit:
                    retry_after = max(1, math.ceil(row[1] - now))
                elif row:
                    conn.execute("UPDATE request_limits SET hits = hits + 1 WHERE key = ?", (key,))
                else:
                    conn.execute("INSERT INTO request_limits VALUES (?, 1, ?)", (key, now + window))
                conn.commit()
                self._initialized = True
                return retry_after
            except BaseException:
                conn.rollback()
                raise


def _client_address():
    value = request.remote_addr or ""
    # ALB can optionally append the client's port to X-Forwarded-For.
    if value.startswith("[") and "]" in value:
        host, suffix = value[1:].split("]", 1)
        value = host if not suffix or (suffix.startswith(":") and suffix[1:].isdigit()) else ""
    elif value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if "." in host and port.isdigit():
            value = host
    try:
        address = ipaddress.ip_address(value)
        return str(getattr(address, "ipv4_mapped", None) or address)
    except ValueError:
        return "unknown"


def _authenticated():
    return bool(session.get("user") and session.get("user") != "guest" and not session.get("is_guest"))


def _request_scope():
    endpoint = request.endpoint or ""
    if request.method == "OPTIONS" or endpoint in ("static", "health_check") or endpoint.startswith("pwa."):
        return None
    if endpoint == "conflicts.api_scan":
        return "scan"
    if endpoint == "admin.upload_status":
        return "poll"
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if endpoint == "chat.chat":
            return "chat" if _authenticated() else "guest_chat"
        return _WRITE_SCOPES.get(endpoint, "write")
    path = request.path
    if ("/api/" in path or path == "/files" or path.startswith((
        "/chat/feedback/", "/chat/conversations", "/chat/conversation/",
        "/chat/escalations/", "/chat/document/",
    ))):
        return "api"
    return None


def init_rate_limiter(app, *, store=None):
    """Install once; explicitly configured proxy hops must be network-trusted."""
    if "request_rate_limiter" in app.extensions:
        return
    enabled = app.config.get("RATE_LIMIT_ENABLED", os.getenv("RATE_LIMIT_ENABLED", "true"))
    if isinstance(enabled, str):
        if enabled.lower() not in ("true", "false", "1", "0"):
            raise ValueError("RATE_LIMIT_ENABLED must be true or false.")
        enabled = enabled.lower() in ("true", "1")
    if not isinstance(enabled, bool):
        raise ValueError("RATE_LIMIT_ENABLED must be a boolean.")
    app.config["RATE_LIMIT_ENABLED"] = enabled

    raw_hops = app.config.get("RATE_LIMIT_PROXY_HOPS", os.getenv("RATE_LIMIT_PROXY_HOPS", "0"))
    if isinstance(raw_hops, bool) or not str(raw_hops).isdigit() or not 0 <= int(raw_hops) <= 5:
        raise ValueError("RATE_LIMIT_PROXY_HOPS must be an integer from 0 to 5.")
    hops = int(raw_hops)
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=0, x_host=0, x_port=0, x_prefix=0)

    overrides = app.config.get("RATE_LIMIT_RULES", os.getenv("RATE_LIMIT_RULES", "{}"))
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    if not isinstance(overrides, dict) or any(name not in DEFAULT_RULES for name in overrides):
        raise ValueError("RATE_LIMIT_RULES must contain only recognized rule names.")
    rules = dict(DEFAULT_RULES)
    for name, value in overrides.items():
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or any(type(number) is not int or number < 1 for number in value)):
            raise ValueError("Each rate rule must be [positive request_count, positive window_seconds].")
        rules[name] = tuple(value)
    app.config["RATE_LIMIT_RULES"] = rules

    path = app.config.get("RATE_LIMIT_STORAGE_PATH") or os.getenv("RATE_LIMIT_STORAGE_PATH")
    if not path:
        runtime = os.getenv("SC_RUNTIME_DB") or os.getenv("PASSWORD_RESET_STATE_PATH")
        if runtime:
            source = Path(runtime)
            path = source.with_name(source.stem + ".rate-limits.sqlite3")
        else:
            workspace = hashlib.sha256(app.root_path.encode()).hexdigest()[:12]
            path = Path(tempfile.gettempdir()) / f"sc-rate-limits-{workspace}" / "requests.sqlite3"
    storage = store if store is not None else SQLiteRateStore(path)
    app.extensions["request_rate_limiter"] = storage
    if enabled and not app.secret_key:
        raise ValueError("Rate limiting requires the application's stable secret key.")

    def reject(seconds, *, unavailable=False):
        message = ("Request protection is temporarily unavailable. Please try again shortly."
                   if unavailable else f"Too many requests. Please try again in {seconds} seconds.")
        response = jsonify(success=False, message=message, error=message,
                           code="rate_limit_storage_unavailable" if unavailable else "rate_limited",
                           retry_after=seconds)
        response.status_code = 503 if unavailable else 429
        response.headers["Retry-After"] = str(seconds)
        response.headers["Cache-Control"] = "no-store"
        return response

    def admit(scope, identity):
        limit, window = rules[scope]
        secret = app.secret_key if isinstance(app.secret_key, bytes) else str(app.secret_key).encode()
        key = hmac.new(secret, f"{scope}\0{window}\0{identity}".encode(), hashlib.sha256).hexdigest()
        try:
            retry_after = storage.consume(key, limit, window)
        except Exception as error:  # noqa: BLE001 - protected work must not run if quota storage fails
            app.logger.error("Rate-limit storage unavailable (%s).", type(error).__name__)
            return reject(3, unavailable=True)
        return reject(retry_after) if retry_after else None

    @app.before_request
    def enforce_request_limits():
        if not app.config["RATE_LIMIT_ENABLED"]:
            return None
        scope = _request_scope()
        if scope is None:
            return None
        address = _client_address()
        endpoint = request.endpoint or ""
        identity = (f"user:{session.get('uid') or session['user']}"
                    if _authenticated() and not endpoint.startswith("auth.") else f"ip:{address}")
        response = admit(scope, identity)
        if response is not None:
            return response
        if endpoint == "auth.login" and request.method == "POST":
            # Apply the IP budget before parsing credentials; never store passwords.
            request.max_content_length = min(request.max_content_length or 16384, 16384)
            identifier = (request.form.get("email") or "").strip().casefold()[:254]
            if identifier:
                return admit("login_target", f"ip:{address}\0target:{identifier}")
        return None
