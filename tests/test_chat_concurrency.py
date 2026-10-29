"""Exercise the actual Flask chat route with fake external RAG/AWS services."""

import ast
import concurrent.futures
import copy
import datetime
import json
import multiprocessing
import os
import queue
import tempfile
import threading
import time
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401
from flask import (
    Blueprint,
    Flask,
    Response,
    jsonify,
    request,
    session,
    stream_with_context,
)
from werkzeug.exceptions import HTTPException

from rag.chat_runtime import ChatRuntime, ConcurrentTurnError
from rag.progress import ProgressChannel
from rag.roles import resolve_role
from rag.runtime import BoundedExecutor, CapacityError, RequestBudget, env_int

_boot_barrier = None


def initialize_guest_worker(barrier):
    global _boot_barrier
    _boot_barrier = barrier


def saved_guest(args):
    global _boot_barrier
    path, ident = args
    if _boot_barrier is not None:
        _boot_barrier.wait(timeout=8)
        _boot_barrier = None
    runtime = ChatRuntime(path)
    runtime.save_guest(f"g-{ident}", [{"role": "user", "content": str(ident)}])


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = ChatRuntime(os.path.join(self.tmp.name, "runtime.sqlite3"))
        # Model the aggregate stream capacity of three12-thread workers, without
        # claiming this single-process fake proves production throughput.
        self.pool = BoundedExecutor(36, 0, "test-answers")
        self.addCleanup(self.pool.shutdown)
        self.preparation = BoundedExecutor(12, 72, "test-preparation")
        self.addCleanup(self.preparation.shutdown)
        env = patch.dict(os.environ, {"CHAT_MAX_ACTIVE": "30", "CHAT_MAX_IMAGES": "2"})
        env.start()
        self.addCleanup(env.stop)
        self.saved = {}
        self.seen = []

        def invoke(payload, config):
            self.seen.append(payload)
            return {"messages_to_llm": [payload], "citations": []}

        def stream(messages):
            yield "Answer: " + messages[0]["input"]

        def save(uid, conv_id, history, created):
            self.saved[(uid, conv_id)] = list(history)

        app = Flask(__name__)
        app.secret_key = "offline-test-secret"
        app.testing = True
        bp = Blueprint("chat", __name__, url_prefix="/chat")
        self.ns = {
            "bp": bp, "jsonify": jsonify, "request": request, "session": session,
            "Response": Response, "stream_with_context": stream_with_context,
            "json": json, "datetime": datetime, "threading": threading,
            "time": time, "uuid": uuid, "os": os, "queue": queue,
            "HTTPException": HTTPException, "RequestBudget": RequestBudget,
            "ConcurrentTurnError": ConcurrentTurnError, "CapacityError": CapacityError,
            "chat_runtime": self.runtime, "answer_pool": self.pool, "preparation_pool": self.preparation,
            "env_int": env_int, "concurrent": concurrent,
            "get_session_id": lambda: session.setdefault("session_id", uuid.uuid4().hex),
            "resolve_role": resolve_role, "ProgressChannel": ProgressChannel,
            "app_graph": SimpleNamespace(invoke=invoke), "stream_answer": stream,
            "looks_unanswered": lambda _: False, "record_gap": lambda *a, **k: None,
            "get_conversation": lambda uid, cid: {"messages": self.saved.get((uid, cid), [])},
            "list_conversations": lambda uid: [],
            "upsert_conversation": save,
        }
        source = ast.parse(Path("sc_assistant/chat.py").read_text(encoding="utf-8"))
        functions = [n for n in source.body if isinstance(n, ast.FunctionDef)
                     and n.name in ("_is_guest", "chat", "conversations", "conversation")]
        helpers = ast.parse(Path("sc_assistant/utils.py").read_text(encoding="utf-8"))
        functions.extend(n for n in helpers.body if isinstance(n, ast.FunctionDef)
                         and n.name == "json_safe_sets")
        reports = ast.parse(Path("sc_assistant/admin_reports.py").read_text(encoding="utf-8"))
        functions.extend(n for n in reports.body if isinstance(n, ast.FunctionDef)
                         and n.name in ("_require_admin", "api_get_reported_conversation"))
        self.ns["bp_reports"] = Blueprint("reports", __name__, url_prefix="/admin/reports")
        self.ns["is_admin"] = lambda: session.get("role") == "admin"
        exec(compile(ast.Module(body=functions, type_ignores=[]), "sc_assistant/chat.py", "exec"), self.ns)  # noqa: S102
        app.register_blueprint(bp)
        app.register_blueprint(self.ns["bp_reports"])
        self.app = app

    def client(self, ident, guest=True):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.update(user="guest" if guest else f"{ident}@example.invalid",
                        is_guest=guest, uid=None if guest else ident,
                        session_id=ident, role="student")
        return client

    def ask(self, ident, text="list the deans", guest=True):
        response = self.client(ident, guest).post("/chat/get", data={"msg": text})
        try:
            return response.status_code, response.get_data(as_text=True)
        finally:
            response.close()

    def test_thirty_sessions_complete_and_thirty_first_is_rejected(self):
        release, started = threading.Event(), threading.Event()
        lock = threading.Lock()
        count = 0

        def invoke(payload, config):
            nonlocal count
            with lock:
                count += 1
                if count == 30:
                    started.set()
            release.wait(8)
            return {"messages_to_llm": [payload], "citations": []}

        self.ns["app_graph"] = SimpleNamespace(invoke=invoke)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=30) as callers:
                futures = [callers.submit(self.ask, f"g-{i}", f"question {i}") for i in range(30)]
                try:
                    self.assertTrue(started.wait(6), "all30 admitted requests should reach the fake pipeline")
                    extra = self.client("extra").post("/chat/get", data={"msg": "one more"})
                    self.assertEqual(extra.status_code, 429)
                    self.assertEqual(extra.headers["Retry-After"], "3")
                finally:
                    release.set()
                outputs = [future.result(timeout=8) for future in futures]
        finally:
            release.set()
        self.assertTrue(all(status == 200 and '"type": "done"' in text for status, text in outputs))
        for i in range(30):
            history = self.runtime.load_guest(f"g-{i}")
            self.assertEqual(history[0]["content"], f"question {i}")
            self.assertEqual(history[1]["content"], f"Answer: question {i}")
        token = self.runtime.acquire("after-completion")
        self.runtime.release(token)

    def test_authenticated_history_is_explicit_and_isolated(self):
        for ident in ("one", "two"):
            client = self.client(ident, guest=False)
            with client.post("/chat/get", data={"msg": ident}) as response:
                self.assertIn('"type": "done"', response.get_data(as_text=True))
            with client.post("/chat/get", data={"msg": "follow up"}) as response:
                self.assertIn('"type": "done"', response.get_data(as_text=True))
            payload = self.seen[-1]
            self.assertEqual(payload["chat_history"][0]["content"], ident)
            self.assertEqual(payload["uid"], ident)
        self.assertEqual(len(self.saved), 2)

    def test_partial_failure_is_not_completed_or_saved(self):
        def stream(messages):
            yield "partial text"
            raise RuntimeError("simulated provider disconnect")

        self.ns["stream_answer"] = stream
        status, text = self.ask("partial")
        self.assertEqual(status, 200)
        self.assertIn("partial text", text)
        self.assertIn('"type": "error"', text)
        self.assertNotIn('"type": "done"', text)
        self.assertEqual(self.runtime.load_guest("partial"), [])

    def test_deadline_and_close_cancel_work_without_saving(self):
        self.ns["RequestBudget"] = lambda: RequestBudget(0.02)

        def invoke(payload, config):
            time.sleep(0.1)
            return {"messages_to_llm": [payload], "citations": []}

        self.ns["app_graph"] = SimpleNamespace(invoke=invoke)
        _, text = self.ask("deadline")
        self.assertIn('"type": "error"', text)
        self.assertNotIn('"type": "done"', text)
        self.pool.shutdown()
        self.assertEqual(self.runtime.load_guest("deadline"), [])

    def test_close_before_pipeline_start_releases_admission(self):
        response = self.client("unread").post("/chat/get", data={"msg": "hello"}, buffered=False)
        response.close()
        token = self.runtime.acquire("session:unread")
        self.runtime.release(token)

    def test_first_turn_cookie_cannot_bypass_the_conversation_lock(self):
        first_client = self.client("first-account", guest=False)
        first = first_client.post("/chat/get", data={"msg": "first"}, buffered=False)
        try:
            with first_client.session_transaction() as sess:
                copied = dict(sess)
            self.assertTrue(copied["current_conv_id"])
            second_client = self.app.test_client()
            with second_client.session_transaction() as sess:
                sess.update(copied)
            second = second_client.post("/chat/get", data={"msg": "overlapping"})
            self.assertEqual(second.status_code, 409)
            self.assertEqual(self.saved, {})
        finally:
            first.close()
        with first_client.post("/chat/get", data={"msg": "retry"}) as response:
            text = response.get_data(as_text=True)
            self.assertIn('"type": "done"', text)
            self.assertIn('"new_conversation_created": true', text)

    def test_account_without_uid_cannot_report_unsaved_success(self):
        client = self.client("invalid-account", guest=False)
        with client.session_transaction() as sess:
            sess.pop("uid")
        response = client.post("/chat/get", data={"msg": "hello"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.saved, {})

    def test_storage_failure_cannot_report_success(self):
        with patch.object(self.runtime, "save_guest", side_effect=RuntimeError("disk full")):
            _, text = self.ask("save-failed")
        self.assertIn('"type": "error"', text)
        self.assertNotIn('"type": "done"', text)

    def test_history_endpoints_serialize_nested_dynamodb_sets(self):
        record = {
            "conv_id": "history-1", "uid": "reader", "title": "School offices",
            "tags": {"school", "offices"}, "rating": Decimal("1.50"),
            "messages": [
                {"role": "user", "content": "List the school offices."},
                {"role": "assistant", "content": "See the handbook.", "citations": [
                    {"source": "handbook.pdf", "pages": {Decimal(10), Decimal(2), Decimal(1)},
                     "sections": {"Registrar", "Admissions"}},
                ]},
            ],
        }
        original = copy.deepcopy(record)
        self.ns["list_conversations"] = lambda uid: [record] if uid == "reader" else []
        self.ns["get_conversation"] = lambda uid, cid: record if (uid, cid) == ("reader", "history-1") else None
        client = self.client("reader", guest=False)
        for path in ("/chat/conversations", "/chat/conversation/history-1"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                result = response.get_json()
                item = result[0] if isinstance(result, list) else result
                self.assertEqual(item["tags"], ["offices", "school"])
                citation = item["messages"][1]["citations"][0]
                self.assertEqual(citation["pages"], ["1", "2", "10"])
                self.assertEqual(citation["sections"], ["Admissions", "Registrar"])
                self.assertEqual(item["rating"], "1.50")
        self.assertEqual(record, original)
        self.assertIsInstance(record["tags"], set)

    def test_history_permissions_and_missing_conversations_are_unchanged(self):
        for client in (self.app.test_client(), self.client("guest-reader")):
            self.assertEqual(client.get("/chat/conversations").get_json(), [])
            self.assertEqual(client.get("/chat/conversation/other").status_code, 401)
        self.ns["get_conversation"] = lambda uid, cid: None
        self.assertEqual(self.client("reader", guest=False).get("/chat/conversation/missing").status_code, 404)

    def test_reported_history_uses_the_same_safe_response_boundary(self):
        self.ns["get_conversation"] = lambda uid, cid: {
            "conv_id": cid, "uid": uid, "messages": [], "tags": {"office", "policy"},
        }
        path = "/admin/reports/api/conversation/reader/history-1"
        self.assertEqual(self.client("reader", guest=False).get(path).status_code, 403)
        admin = self.client("admin", guest=False)
        with admin.session_transaction() as sess:
            sess["role"] = "admin"
        response = admin.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["tags"], ["office", "policy"])

    def test_json_collection_conversion_keeps_lists_and_scalars_intact(self):
        source = {"messages": ["second", "first"], "empty": set(), "frozen": frozenset({"b", "a"}),
                  "tuple": ({"nested": {3, 1}},), "nothing": None, "flag": False,
                  "precise": Decimal("0.1000000000000000001")}
        converted = self.ns["json_safe_sets"](source)
        self.assertEqual(converted["messages"], ["second", "first"])
        self.assertEqual(converted["empty"], [])
        self.assertEqual(converted["frozen"], ["a", "b"])
        self.assertEqual(converted["tuple"], [{"nested": [1, 3]}])
        self.assertIsNone(converted["nothing"])
        self.assertIs(converted["flag"], False)
        self.assertIs(converted["precise"], source["precise"])
        self.assertIsInstance(source["tuple"][0]["nested"], set)

    def test_guest_history_is_shared_between_processes_and_expires(self):
        context = multiprocessing.get_context("spawn")
        with context.Pool(3, initializer=initialize_guest_worker,
                          initargs=(context.Barrier(3),)) as pool:
            pool.map(saved_guest, [(self.runtime.path, i) for i in range(30)])
        other = ChatRuntime(self.runtime.path)
        for i in range(30):
            self.assertEqual(other.load_guest(f"g-{i}")[0]["content"], str(i))
        with patch("rag.chat_runtime.time.time", return_value=time.time() + 8000):
            self.assertEqual(other.load_guest("g-1"), [])

    def test_admission_is_shared_and_same_conversation_is_locked(self):
        other = ChatRuntime(self.runtime.path)
        token = self.runtime.acquire("same")
        try:
            with self.assertRaises(ConcurrentTurnError):
                other.acquire("same")
            first = other.acquire("image-1", image=True)
            second = self.runtime.acquire("image-2", image=True)
            try:
                with self.assertRaises(CapacityError):
                    other.acquire("image-3", image=True)
            finally:
                other.release(first)
                other.release(second)
        finally:
            self.runtime.release(token)


if __name__ == "__main__":
    unittest.main()
