"""Offline rate-limit contracts, without importing the application or config."""

import importlib.util
import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from flask import Flask, jsonify, request


@contextmanager
def offline_only():
    forbidden = dict.fromkeys((
        "sc_assistant", "config", "dotenv", "aws", "rag", "src",
        "boto3", "botocore", "pinecone",
    ))
    denied = AssertionError("Network access is forbidden in rate-limit tests")
    with patch.dict(sys.modules, forbidden), \
            patch("socket.create_connection", side_effect=denied), \
            patch("socket.socket.connect", side_effect=denied), \
            patch("socket.socket.connect_ex", side_effect=denied), \
            patch("socket.getaddrinfo", side_effect=denied):
        yield


def load_rate_limits():
    # A package import would execute sc_assistant/__init__.py and initialize services.
    path = Path(__file__).resolve().parents[1] / "sc_assistant" / "rate_limit.py"
    spec = importlib.util.spec_from_file_location("_rate_limits_under_test", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load standalone rate limiter: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with offline_only():
        spec.loader.exec_module(module)
    return module


rate_limits = load_rate_limits()
RULE_NAMES = (
    "api", "write", "chat", "guest_chat", "login", "login_target", "signup",
    "code_send", "verify", "feedback", "report", "upload", "scan", "poll",
)


class Clock:
    def __init__(self, now=1200.0):
        self.now = now

    def __call__(self):
        return self.now


def rules(**overrides):
    return {**{name: [1000, 60] for name in RULE_NAMES}, **overrides}


def database_rows(path):
    # Inspect cleanup/privacy without depending on a private table or column name.
    with closing(sqlite3.connect(str(path))) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        rows = []
        for (table,) in tables:
            quoted = table.replace('"', '""')
            rows.extend(connection.execute(f'SELECT * FROM "{quoted}"').fetchall())
        return rows


def make_app(store, **config):
    app = Flask(__name__, static_folder=None)
    app.secret_key = "test-only-secret"
    app.config.update(TESTING=True)
    app.config.update(config)
    calls = []

    def handler(**values):
        calls.append((request.endpoint, request.method))
        return jsonify(
            success=True, address=request.remote_addr, scheme=request.scheme,
            host=request.host, script_root=request.script_root,
        )

    routes = (
        ("chat.chat", ("/chat/get", "/get"), ("POST",)),
        ("auth.login", ("/login", "/auth/login"), ("POST",)),
        ("auth.signup", ("/signup",), ("GET", "POST")),
        ("auth.handle_forgot_password", ("/forgot-password",), ("POST",)),
        ("auth.resend_signup", ("/resend-signup-code",), ("POST",)),
        ("auth.confirm_signup", ("/confirm-signup",), ("POST",)),
        ("auth.handle_reset_password", ("/reset-password",), ("POST",)),
        ("settings.change_password", ("/settings/change-password",), ("POST",)),
        ("chat.submit_feedback", ("/chat/feedback",), ("POST",)),
        ("chat.submit_report", ("/chat/report",), ("POST",)),
        ("chat.escalate", ("/chat/escalate",), ("POST",)),
        ("admin.upload_file", ("/upload",), ("POST",)),
        ("conflicts.api_scan", ("/admin/conflicts/api/scan",), ("GET", "POST")),
        ("freshness.api_scan_upload", ("/admin/freshness/api/scan-upload",), ("POST",)),
        ("admin.upload_status", ("/upload/status/<job_id>",), ("GET",)),
        ("chat.read_feedback_batch", ("/chat/feedback/batch",), ("POST",)),
        ("chat.conversations", ("/chat/conversations",), ("GET",)),
        ("reports.api_list", ("/admin/reports/api/list",), ("GET",)),
        ("api_items", ("/api/items",), ("GET", "POST", "PUT", "PATCH", "DELETE")),
        ("settings.update_profile", ("/settings/update-profile",), ("POST", "PUT", "PATCH", "DELETE")),
        ("home", ("/",), ("GET",)),
        ("auth.auth_page", ("/auth",), ("GET",)),
        ("admin.dashboard", ("/dashboard",), ("GET",)),
        ("health_check", ("/health", "/api/health"), ("GET",)),
        ("static", ("/static/test.css",), ("GET",)),
        ("pwa.manifest", ("/manifest.webmanifest",), ("GET",)),
        ("pwa.service_worker", ("/sw.js",), ("GET",)),
        ("pwa.offline", ("/offline",), ("GET",)),
    )
    for endpoint, paths, methods in routes:
        for path in paths:
            app.add_url_rule(path, endpoint, handler, methods=list(methods))
    rate_limits.init_rate_limiter(app, store=store)
    return app, calls


def signed_in_client(app, uid="student-one", user="student@example.invalid"):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user=user, is_guest=False, session_id="first-cookie")
        if uid is not None:
            session["uid"] = uid
    return client


_worker_barrier = None


def initialize_worker(barrier):
    global _worker_barrier
    _worker_barrier = barrier


def consume_in_worker(path):
    with offline_only():
        store = rate_limits.SQLiteRateStore(path, clock=Clock())
        _worker_barrier.wait(timeout=20)
        return os.getpid(), [store.consume("shared", 7, 60) for _ in range(8)]


class RateLimitTestCase(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.resources.enter_context(offline_only())
        self.tmp = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp) / "limits.sqlite3"
        self.clock = Clock()

    def store(self, name="limits"):
        return rate_limits.SQLiteRateStore(
            str(Path(self.tmp) / f"{name}.sqlite3"), clock=self.clock,
        )

    def assert_limited(self, response, retry_after=None):
        self.assertEqual(response.status_code, 429)
        retry = response.headers.get("Retry-After", "")
        self.assertTrue(retry.isdigit(), retry)
        self.assertGreater(int(retry), 0)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        if retry_after is not None:
            self.assertEqual(int(retry), retry_after)
        if response.request.method == "HEAD":
            self.assertEqual(response.data, b"")
            return
        self.assertTrue(response.is_json)
        payload = response.get_json()
        self.assertIs(payload["success"], False)
        self.assertEqual(payload["code"], "rate_limited")
        self.assertIsInstance(payload["message"], str)
        self.assertTrue(payload["message"].strip())
        self.assertNotEqual(payload["message"], payload["code"])
        self.assertEqual(payload["error"], payload["message"])
        self.assertIs(type(payload["retry_after"]), int)
        self.assertEqual(payload["retry_after"], int(retry))


class SQLiteRateStoreTests(RateLimitTestCase):
    def test_construction_is_lazy_and_first_consume_initializes_tables(self):
        store = self.store()
        self.assertFalse(self.path.exists())
        self.assertEqual(store.consume("first", 1, 60), 0)
        self.assertTrue(self.path.is_file())
        self.assertTrue(database_rows(self.path))

    def test_quota_ceiling_and_exact_window_boundary(self):
        store = self.store()
        for _ in range(2):
            result = store.consume("quota", 2, 60)
            self.assertIs(type(result), int)
            self.assertEqual(result, 0)
        for now, expected in ((1200, 60), (1200.25, 60), (1258.25, 2), (1259.999, 1)):
            with self.subTest(now=now):
                self.clock.now = now
                result = store.consume("quota", 2, 60)
                self.assertIs(type(result), int)
                self.assertEqual(result, expected)
        self.clock.now = 1260
        self.assertEqual(store.consume("quota", 2, 60), 0)
        self.assertEqual(store.consume("quota", 2, 60), 0)
        self.assertEqual(store.consume("quota", 2, 60), 60)

    def test_keys_and_window_lengths_are_independent(self):
        store = self.store()
        self.assertEqual(store.consume("long", 1, 60), 0)
        self.assertEqual(store.consume("short", 2, 30), 0)
        self.assertEqual(store.consume("short", 2, 30), 0)
        self.assertEqual(store.consume("short", 2, 30), 30)
        self.clock.now = 1230
        self.assertEqual(store.consume("short", 2, 30), 0)
        self.assertEqual(store.consume("long", 1, 60), 30)

    def test_new_store_instances_do_not_reset_a_shared_quota(self):
        self.assertEqual(self.store().consume("shared", 2, 60), 0)
        self.assertEqual(self.store().consume("shared", 2, 60), 0)
        self.assertEqual(self.store().consume("shared", 2, 60), 60)

    def test_later_consumption_cleans_expired_counters_but_keeps_live_ones(self):
        store = self.store()
        for number in range(24):
            self.assertEqual(store.consume(f"expired-{number}", 1, 60), 0)
        self.assertEqual(store.consume("still-live", 1, 600), 0)
        before = len(database_rows(self.path))
        self.clock.now = 1261
        self.assertEqual(store.consume("new", 1, 60), 0)
        self.assertLess(len(database_rows(self.path)), before)
        self.assertEqual(store.consume("still-live", 1, 600), 539)
        self.assertEqual(store.consume("expired-0", 1, 60), 0)

    def test_corrupt_database_errors_propagate_without_replacing_the_file(self):
        original = b"This is deliberately not a SQLite database.\n"
        self.path.write_bytes(original)
        store = self.store()
        with self.assertRaises(sqlite3.DatabaseError):
            store.consume("quota", 1, 60)
        self.assertEqual(self.path.read_bytes(), original)

    def test_database_open_errors_propagate(self):
        store = rate_limits.SQLiteRateStore(self.tmp, clock=self.clock)
        with self.assertRaises((sqlite3.Error, OSError)):
            store.consume("quota", 1, 60)

    def test_independent_processes_atomically_share_a_cold_database(self):
        context = multiprocessing.get_context("spawn")
        self.assertFalse(self.path.exists())
        with context.Pool(4, initializer=initialize_worker,
                          initargs=(context.Barrier(4),)) as pool:
            results = pool.map_async(
                consume_in_worker, [str(self.path)] * 4, chunksize=1,
            ).get(timeout=45)
        self.assertEqual(len({pid for pid, _ in results}), 4)
        decisions = [decision for _, batch in results for decision in batch]
        self.assertEqual(decisions.count(0), 7)
        self.assertEqual(decisions.count(60), 25)
        self.assertTrue(all(type(decision) is int for decision in decisions))
        self.assertEqual(self.store().consume("shared", 7, 60), 60)
        self.clock.now = 1260
        self.assertEqual(self.store().consume("shared", 7, 60), 0)


class FlaskRateLimitTests(RateLimitTestCase):
    def test_default_configuration_enables_request_accounting(self):
        store = self.store()
        with patch.object(store, "consume", wraps=store.consume) as consume:
            app, calls = make_app(store)
            self.assertFalse(self.path.exists())
            self.assertEqual(app.test_client().get("/api/items").status_code, 200)
            self.assertTrue(consume.called)
        self.assertEqual(len(calls), 1)

    def test_false_disables_limiting_without_touching_storage(self):
        store = self.store()
        with patch.object(store, "consume", side_effect=AssertionError("Storage used while disabled")) as consume:
            app, calls = make_app(store, RATE_LIMIT_ENABLED=False, RATE_LIMIT_RULES=rules(guest_chat=[1, 60]))
            client = app.test_client()
            for _ in range(3):
                self.assertEqual(client.post("/chat/get").status_code, 200)
            consume.assert_not_called()
        self.assertEqual(len(calls), 3)
        self.assertFalse(self.path.exists())

    def test_endpoint_and_method_select_the_expected_rule(self):
        cases = (
            ("POST", "/chat/get", "chat", True),
            ("POST", "/chat/get", "guest_chat", False),
            ("POST", "/login", "login", False),
            ("POST", "/signup", "signup", False),
            ("POST", "/forgot-password", "code_send", False),
            ("POST", "/resend-signup-code", "code_send", False),
            ("POST", "/confirm-signup", "verify", False),
            ("POST", "/reset-password", "verify", False),
            ("POST", "/settings/change-password", "verify", True),
            ("POST", "/chat/feedback", "feedback", True),
            ("POST", "/chat/report", "report", True),
            ("POST", "/chat/escalate", "report", True),
            ("POST", "/upload", "upload", True),
            ("GET", "/admin/conflicts/api/scan", "scan", True),
            ("HEAD", "/admin/conflicts/api/scan", "scan", True),
            ("POST", "/admin/conflicts/api/scan", "scan", True),
            ("POST", "/admin/freshness/api/scan-upload", "scan", True),
            ("GET", "/upload/status/job-one", "poll", True),
            ("GET", "/api/items", "api", False),
            ("HEAD", "/api/items", "api", False),
            ("GET", "/admin/reports/api/list", "api", True),
            ("GET", "/chat/conversations", "api", True),
            ("POST", "/chat/feedback/batch", "api", True),
            ("POST", "/api/items", "write", True),
            ("POST", "/settings/update-profile", "write", True),
            ("PUT", "/settings/update-profile", "write", True),
            ("PATCH", "/settings/update-profile", "write", True),
            ("DELETE", "/settings/update-profile", "write", True),
        )
        for index, (method, path, rule, authenticated) in enumerate(cases):
            with self.subTest(method=method, path=path, rule=rule):
                app, calls = make_app(self.store(f"route-{index}"), RATE_LIMIT_RULES=rules(**{rule: [2, 60]}))
                client = signed_in_client(app) if authenticated else app.test_client()
                for _ in range(2):
                    self.assertEqual(client.open(path, method=method).status_code, 200)
                self.assert_limited(client.open(path, method=method), 60)
                self.assertEqual(len(calls), 2, "Denied requests must not execute the handler")

    def test_aliases_and_grouped_endpoints_share_their_bucket(self):
        cases = (
            ("guest_chat", (("POST", "/chat/get"), ("POST", "/get"))),
            ("login", (("POST", "/login"), ("POST", "/auth/login"))),
            ("code_send", (("POST", "/forgot-password"), ("POST", "/resend-signup-code"))),
            ("verify", (("POST", "/confirm-signup"), ("POST", "/reset-password"))),
            ("report", (("POST", "/chat/report"), ("POST", "/chat/escalate"))),
            ("scan", (("GET", "/admin/conflicts/api/scan"), ("HEAD", "/admin/conflicts/api/scan"),
                      ("POST", "/admin/conflicts/api/scan"), ("POST", "/admin/freshness/api/scan-upload"))),
            ("poll", (("GET", "/upload/status/job-one"), ("GET", "/upload/status/job-two"))),
        )
        for index, (rule, routes) in enumerate(cases):
            with self.subTest(rule=rule):
                app, calls = make_app(self.store(f"group-{index}"), RATE_LIMIT_RULES=rules(**{rule: [1, 60]}))
                client = app.test_client()
                method, path = routes[0]
                self.assertEqual(client.open(path, method=method).status_code, 200)
                for method, path in routes[1:]:
                    self.assert_limited(client.open(path, method=method), 60)
                self.assertEqual(len(calls), 1)

    def test_feedback_batch_post_uses_api_not_write_quota(self):
        app, calls = make_app(self.store(), RATE_LIMIT_RULES=rules(api=[1, 60], write=[1, 60]))
        client = signed_in_client(app)
        self.assertEqual(client.post("/settings/update-profile").status_code, 200)
        self.assert_limited(client.delete("/api/items"))
        self.assertEqual(client.post("/chat/feedback/batch").status_code, 200)
        self.assert_limited(client.get("/api/items"))
        self.assertEqual(len(calls), 2)

    def test_upload_and_poll_have_separate_budgets(self):
        app, calls = make_app(self.store(), RATE_LIMIT_RULES=rules(upload=[1, 60], poll=[2, 60]))
        client = signed_in_client(app)
        self.assertEqual(client.post("/upload").status_code, 200)
        self.assert_limited(client.post("/upload"))
        self.assertEqual(client.get("/upload/status/one").status_code, 200)
        self.assertEqual(client.get("/upload/status/one").status_code, 200)
        self.assert_limited(client.get("/upload/status/two"))
        self.assertEqual(len(calls), 3)

    def test_authenticated_identity_uses_stable_uid_across_sessions_and_ips(self):
        app, calls = make_app(self.store(), RATE_LIMIT_RULES=rules(chat=[1, 60]))
        first = signed_in_client(app, uid="one", user="old-name@example.invalid")
        self.assertEqual(first.post("/chat/get").status_code, 200)
        same = signed_in_client(app, uid="one", user="new-name@example.invalid")
        with same.session_transaction() as session:
            session["session_id"] = "replacement-cookie"
            session["current_conv_id"] = "replacement-conversation"
        self.assert_limited(same.post("/get", environ_overrides={"REMOTE_ADDR": "198.51.100.24"}))
        other = signed_in_client(app, uid="two", user="old-name@example.invalid")
        self.assertEqual(other.post("/chat/get").status_code, 200)
        self.assertEqual(len(calls), 2)

    def test_authenticated_user_fallback_is_stable_when_uid_is_absent(self):
        app, _ = make_app(self.store(), RATE_LIMIT_RULES=rules(chat=[1, 60]))
        first = signed_in_client(app, uid=None, user="one@example.invalid")
        with first.session_transaction() as session:
            session.pop("is_guest")
        self.assertEqual(first.post("/chat/get").status_code, 200)
        same = signed_in_client(app, uid=None, user="one@example.invalid")
        self.assert_limited(same.post("/chat/get", environ_overrides={"REMOTE_ADDR": "198.51.100.25"}))
        other = signed_in_client(app, uid=None, user="two@example.invalid")
        self.assertEqual(other.post("/chat/get").status_code, 200)

    def test_guest_cookies_uid_only_sessions_and_session_reset_cannot_bypass_ip_quota(self):
        app, calls = make_app(self.store(), RATE_LIMIT_RULES=rules(guest_chat=[1, 60]))
        first = app.test_client()
        self.assertEqual(first.post("/chat/get").status_code, 200)
        for state in (
            {},
            {"user": "guest", "is_guest": True, "uid": "invented-one", "session_id": "cookie-one"},
            {"user": "other@example.invalid", "is_guest": True, "uid": "invented-two", "session_id": "cookie-two"},
            {"uid": "not-authenticated", "is_guest": False},
        ):
            with self.subTest(state=state):
                client = app.test_client()
                with client.session_transaction() as session:
                    session.update(state)
                self.assert_limited(client.post("/chat/get"))
        with first.session_transaction() as session:
            session.clear()
        self.assert_limited(first.post("/get"))
        self.assertEqual(signed_in_client(app).post("/chat/get").status_code, 200)
        self.assertEqual(app.test_client().post(
            "/chat/get", environ_overrides={"REMOTE_ADDR": "198.51.100.30"},
        ).status_code, 200)
        self.assertEqual(len(calls), 3)

    def test_authentication_operations_use_ip_even_for_signed_in_sessions(self):
        cases = (
            ("/login", "login"), ("/signup", "signup"),
            ("/forgot-password", "code_send"), ("/resend-signup-code", "code_send"),
            ("/confirm-signup", "verify"), ("/reset-password", "verify"),
        )
        for index, (path, rule) in enumerate(cases):
            with self.subTest(path=path):
                app, _ = make_app(self.store(f"auth-{index}"), RATE_LIMIT_RULES=rules(**{rule: [1, 60]}))
                first = signed_in_client(app, uid="one")
                other = signed_in_client(app, uid="two")
                self.assertEqual(first.post(path, data={"email": "one@example.invalid"}).status_code, 200)
                self.assert_limited(other.post(path, data={"email": "two@example.invalid"}))
                self.assertEqual(first.post(
                    path, data={"email": "one@example.invalid"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.31"},
                ).status_code, 200)

    def test_settings_password_verification_uses_authenticated_identity(self):
        app, _ = make_app(self.store(), RATE_LIMIT_RULES=rules(verify=[1, 60]))
        first = signed_in_client(app, uid="one")
        self.assertEqual(first.post("/settings/change-password").status_code, 200)
        same = signed_in_client(app, uid="one")
        self.assert_limited(same.post(
            "/settings/change-password", environ_overrides={"REMOTE_ADDR": "198.51.100.32"},
        ))
        self.assertEqual(signed_in_client(app, uid="two").post("/settings/change-password").status_code, 200)

    def test_login_target_normalizes_form_email_and_includes_ip_not_password(self):
        app, calls = make_app(self.store(), RATE_LIMIT_RULES=rules(login_target=[1, 60]))
        client = app.test_client()
        self.assertEqual(client.post("/login", data={
            "email": "  Student@Example.Invalid  ", "password": "test-password-one",
        }).status_code, 200)
        self.assert_limited(client.post("/auth/login", data={
            "email": "student@example.invalid", "password": "test-password-two",
        }))
        self.assertEqual(client.post("/login", data={"email": "other@example.invalid"}).status_code, 200)
        self.assertEqual(client.post(
            "/login", data={"email": "student@example.invalid"},
            environ_overrides={"REMOTE_ADDR": "198.51.100.33"},
        ).status_code, 200)
        self.assertEqual(len(calls), 3)

    def test_html_signup_get_is_exempt_but_post_is_limited(self):
        store = self.store()
        app, _ = make_app(store, RATE_LIMIT_RULES=rules(signup=[1, 60]))
        client = app.test_client()
        with patch.object(store, "consume", wraps=store.consume) as consume:
            for _ in range(3):
                self.assertEqual(client.get("/signup").status_code, 200)
            consume.assert_not_called()
        self.assertEqual(client.post("/signup").status_code, 200)
        self.assert_limited(client.post("/signup"))
        self.assertEqual(client.get("/signup").status_code, 200)

    def test_html_health_static_pwa_and_options_never_access_storage(self):
        store = self.store()
        with patch.object(store, "consume", side_effect=AssertionError("Exempt request used storage")) as consume:
            app, _ = make_app(store)
            client = app.test_client()
            for path in ("/", "/auth", "/signup", "/dashboard", "/health", "/api/health",
                         "/static/test.css", "/manifest.webmanifest", "/sw.js", "/offline"):
                for method in ("GET", "HEAD"):
                    with self.subTest(path=path, method=method):
                        self.assertEqual(client.open(path, method=method).status_code, 200)
            for path in ("/chat/get", "/login", "/api/items", "/admin/conflicts/api/scan"):
                with self.subTest(path=path, method="OPTIONS"):
                    self.assertEqual(client.options(path).status_code, 200)
            consume.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_forwarded_headers_cannot_change_default_client_identity(self):
        app, _ = make_app(self.store(), RATE_LIMIT_RULES=rules(guest_chat=[1, 60]))
        client = app.test_client()
        response = client.post("/chat/get", headers={"X-Forwarded-For": "203.0.113.1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["address"], "127.0.0.1")
        self.assert_limited(client.post("/chat/get", headers={
            "X-Forwarded-For": "203.0.113.2", "X-Real-IP": "203.0.113.3",
            "Forwarded": "for=203.0.113.4",
        }))
        self.assertEqual(client.post(
            "/chat/get", headers={"X-Forwarded-For": "203.0.113.1"},
            environ_overrides={"REMOTE_ADDR": "198.51.100.34"},
        ).status_code, 200)

    def test_explicit_proxy_trusts_only_rightmost_for_and_no_other_forwarded_values(self):
        for index, hops in enumerate((1, "1")):
            with self.subTest(hops=hops):
                app, _ = make_app(self.store(f"proxy-{index}"), RATE_LIMIT_PROXY_HOPS=hops,
                                  RATE_LIMIT_RULES=rules(guest_chat=[1, 60]))
                client = app.test_client()
                headers = {
                    "X-Forwarded-For": "203.0.113.1, 198.51.100.40",
                    "X-Forwarded-Proto": "https", "X-Forwarded-Host": "untrusted.example.invalid",
                    "X-Forwarded-Port": "444", "X-Forwarded-Prefix": "/untrusted",
                }
                response = client.post("/chat/get", headers=headers)
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(body["address"], "198.51.100.40")
                self.assertEqual(body["scheme"], "http")
                self.assertEqual(body["host"], "localhost")
                self.assertEqual(body["script_root"], "")
                headers["X-Forwarded-For"] = "203.0.113.2, 198.51.100.40"
                self.assert_limited(client.post("/chat/get", headers=headers))
                headers["X-Forwarded-For"] = "203.0.113.2, 198.51.100.41"
                self.assertEqual(client.post("/chat/get", headers=headers).status_code, 200)

    def test_equivalent_ip_spellings_and_optional_ports_share_quota(self):
        addresses = (
            ("198.51.100.50", "::ffff:198.51.100.50"),
            ("198.51.100.50", "198.51.100.50:49152"),
            ("198.51.100.50", "[::ffff:198.51.100.50]:49152"),
            ("2001:db8::1", "[2001:db8::1]:49152"),
            ("2001:db8::1", "2001:0db8:0:0:0:0:0:1"),
        )
        for hops in (0, 1):
            for index, (canonical, equivalent) in enumerate(addresses):
                with self.subTest(hops=hops, equivalent=equivalent):
                    app, _ = make_app(self.store(f"address-{hops}-{index}"), RATE_LIMIT_PROXY_HOPS=hops,
                                      RATE_LIMIT_RULES=rules(guest_chat=[1, 60]))
                    client = app.test_client()
                    responses = []
                    for address in (canonical, equivalent):
                        options = ({"headers": {"X-Forwarded-For": address}} if hops else
                                   {"environ_overrides": {"REMOTE_ADDR": address}})
                        responses.append(client.post("/chat/get", **options))
                    self.assertEqual(responses[0].status_code, 200)
                    self.assert_limited(responses[1])

    def test_retry_response_uses_injected_clock_and_does_not_extend_the_window(self):
        app, calls = make_app(self.store(), RATE_LIMIT_RULES=rules(guest_chat=[1, 60]))
        client = app.test_client()
        self.assertEqual(client.post("/chat/get").status_code, 200)
        self.clock.now = 1200.1
        self.assert_limited(client.post("/chat/get"), 60)
        self.clock.now = 1259.9
        self.assert_limited(client.post("/chat/get"), 1)
        self.assertEqual(len(calls), 1)
        self.clock.now = 1260
        self.assertEqual(client.post("/chat/get").status_code, 200)
        self.assertEqual(len(calls), 2)

    def test_storage_failure_returns_503_and_never_executes_the_handler(self):
        store = self.store()
        app, calls = make_app(store, RATE_LIMIT_RULES=rules(guest_chat=[1, 60]))
        client = app.test_client()
        with patch.object(store, "consume", side_effect=sqlite3.OperationalError("synthetic storage failure")):
            for _ in range(2):
                response = client.post("/chat/get")
                self.assertEqual(response.status_code, 503)
                retry = response.headers.get("Retry-After", "")
                self.assertTrue(retry.isdigit(), retry)
                self.assertGreater(int(retry), 0)
                self.assertEqual(calls, [])
        self.assertEqual(client.post("/chat/get").status_code, 200)
        self.assertEqual(len(calls), 1)

    def test_separate_app_workers_and_store_instances_share_accounting(self):
        config = rules(guest_chat=[2, 60])
        all_calls = []
        for expected in (200, 200, 429):
            app, calls = make_app(self.store(), RATE_LIMIT_RULES=config)
            response = app.test_client().post("/chat/get")
            self.assertEqual(response.status_code, expected)
            if expected == 429:
                self.assert_limited(response, 60)
            all_calls.extend(calls)
        self.assertEqual(len(all_calls), 2)

    def test_database_identifiers_are_secret_keyed_and_contain_no_plaintext_pii(self):
        def recorded(name, secret, password):
            app, _ = make_app(self.store(name), SECRET_KEY=secret, RATE_LIMIT_RULES=rules())
            client = signed_in_client(app, uid=None, user="private-student@example.invalid")
            remote = {"REMOTE_ADDR": "198.51.100.60"}
            self.assertEqual(client.post("/login", environ_overrides=remote, data={
                "email": "  Private-Student@Example.Invalid ", "password": password,
            }).status_code, 200)
            self.assertEqual(client.post("/chat/get", environ_overrides=remote).status_code, 200)
            values = {value for row in database_rows(Path(self.tmp) / f"{name}.sqlite3")
                      for value in row if isinstance(value, str)}
            self.assertTrue(values)
            text = "\n".join(values).lower()
            for sensitive in ("198.51.100.60", "private-student@example.invalid", password, secret):
                self.assertNotIn(sensitive.lower(), text)
            return values

        first = recorded("first", "test-only-secret", "test-password-one")
        self.assertEqual(first, recorded("same-secret", "test-only-secret", "test-password-two"))
        self.assertNotEqual(first, recorded("other-secret", "other-test-only-secret", "test-password-one"))

    def test_dict_and_json_partial_overrides_preserve_other_default_rules(self):
        for index, config in enumerate(({"guest_chat": [1, 60]}, '{"guest_chat": [1, 60]}')):
            with self.subTest(config=config):
                store = self.store(f"partial-{index}")
                app, _ = make_app(store, RATE_LIMIT_RULES=config)
                client = app.test_client()
                self.assertEqual(client.post("/chat/get").status_code, 200)
                self.assert_limited(client.post("/chat/get"), 60)
                with patch.object(store, "consume", wraps=store.consume) as consume:
                    self.assertEqual(client.get("/api/items").status_code, 200)
                    self.assertTrue(consume.called, "Partial overrides must not remove default rules")

    def test_invalid_rules_fail_during_initialization(self):
        invalid = (
            {"unknown_rule": [1, 60]}, [], None, 1, True, "", "{", "[]", "null", "false",
            {"api": []}, {"api": [1]}, {"api": [1, 60, 2]}, {"api": "1/60"},
            {"api": {"limit": 1, "window": 60}}, {"api": None},
            {"api": [0, 60]}, {"api": [-1, 60]}, {"api": [1, 0]}, {"api": [1, -60]},
            {"api": [True, 60]}, {"api": [1, False]}, {"api": [1.5, 60]},
            {"api": [1, 60.5]}, {"api": ["1", 60]}, {"api": [1, "60"]},
            json.dumps({"unknown_rule": [1, 60]}), json.dumps({"api": [1, 0]}),
        )
        for index, config in enumerate(invalid):
            with self.subTest(config=config):
                with self.assertRaises((TypeError, ValueError)):
                    make_app(self.store(f"invalid-rule-{index}"), RATE_LIMIT_RULES=config)

    def test_malformed_or_negative_proxy_counts_fail_during_initialization(self):
        for index, hops in enumerate((-1, "-1", "", "invalid", "1.5", 1.5, None, [], True)):
            with self.subTest(hops=hops):
                with self.assertRaises((TypeError, ValueError)):
                    make_app(self.store(f"invalid-proxy-{index}"), RATE_LIMIT_PROXY_HOPS=hops)


if __name__ == "__main__":
    unittest.main()
