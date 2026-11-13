"""Exercise the actual Flask chat route with fake external RAG/AWS services."""

import ast
import concurrent.futures
import copy
import datetime
import io
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
from unittest.mock import Mock, patch

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
from PIL import Image
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
        self.guest_markers = set()
        marker_lock = threading.Lock()
        self.topic_records = {}
        topic_lock = threading.Lock()

        def invoke(payload, config):
            self.seen.append(payload)
            return {"messages_to_llm": [payload], "citations": []}

        def stream(messages):
            yield "Answer: " + messages[0]["input"]

        def save(uid, conv_id, history, created):
            self.saved[(uid, conv_id)] = list(history)

        def record_guest_conversation(session_id):
            # Model durable deduplication without storing any guest transcript.
            with marker_lock:
                self.guest_markers.add(session_id)

        def record_topic_usage(topic, event_id):
            # A strict two-argument fake keeps question/context out of accounting.
            with topic_lock:
                if event_id in self.topic_records:
                    return False
                self.topic_records[event_id] = topic
                return True

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
            "record_guest_conversation": record_guest_conversation,
            "classify_topic": Mock(return_value="enrollment"),
            "record_topic_usage": Mock(side_effect=record_topic_usage),
            "suggested_questions": Mock(return_value=["Safe question one?", "Safe question two?", "Safe question three?"]),
            "Image": Image, "encode_image": Mock(return_value="test-encoded-image"),
        }
        source = ast.parse(Path("sc_assistant/chat.py").read_text(encoding="utf-8"))
        functions = [n for n in source.body if isinstance(n, ast.FunctionDef)
                      and n.name in ("_is_guest", "chat", "chat_suggestions", "clear_memory", "conversations", "conversation")]
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

    def test_suggestions_are_safe_read_only_and_available_to_chat_users(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/chat/api/suggestions").status_code, 401)
        self.ns["suggested_questions"].assert_not_called()
        for role in ("guest", "student", "faculty", "admin"):
            client = self.client(role, guest=role == "guest")
            with client.session_transaction() as sess:
                sess["role"] = role
            response = client.get("/chat/api/suggestions")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(set(response.json), {"success", "suggestions"})
            self.assertEqual(response.json["suggestions"], self.ns["suggested_questions"].return_value)
            self.assertEqual(client.post("/chat/api/suggestions").status_code, 405)
        self.assertEqual(self.guest_markers, set())
        self.assertEqual(self.topic_records, {})

    def test_suggestions_refresh_instead_of_reusing_an_old_ranking(self):
        self.ns["suggested_questions"].side_effect = [["First ranking?"], ["New ranking?"]]
        client = self.client("guest")
        self.assertEqual(client.get("/chat/api/suggestions").json["suggestions"], ["First ranking?"])
        self.assertEqual(client.get("/chat/api/suggestions").json["suggestions"], ["New ranking?"])

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

    def done_event(self, text):
        events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
        self.assertNotIn("error", [event["type"] for event in events])
        done = [event for event in events if event["type"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(set(done[0]), {
            "type", "conv_id", "new_conversation_created", "new_conv_title",
            "unanswered", "citations", "msg_id",
        })
        return done[0]

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
                    extra.close()
                    self.assertEqual(self.guest_markers, set())
                    self.assertEqual(self.topic_records, {})
                    self.ns["classify_topic"].assert_not_called()
                finally:
                    release.set()
                outputs = [future.result(timeout=8) for future in futures]
        finally:
            release.set()
        self.assertTrue(all(status == 200 and '"type": "done"' in text for status, text in outputs))
        topic_ids = set()
        for i in range(30):
            history = self.runtime.load_guest(f"g-{i}")
            self.assertEqual(history[0]["content"], f"question {i}")
            self.assertEqual(history[1]["content"], f"Answer: question {i}")
            done = self.done_event(outputs[i][1])
            self.assertEqual(history[1]["msg_id"], done["msg_id"])
            topic_ids.add(done["msg_id"])
        self.assertEqual(len(topic_ids), 30)
        self.assertEqual(self.topic_records, dict.fromkeys(topic_ids, "enrollment"))
        self.assertEqual(self.ns["record_topic_usage"].call_count, 30)
        self.assertEqual(self.ns["classify_topic"].call_count, 30)
        self.assertEqual(
            {call.args[0] for call in self.ns["classify_topic"].call_args_list},
            {f"question {i}" for i in range(30)},
        )
        self.assertEqual(self.runtime.load_guest("extra"), [])
        self.assertEqual(self.guest_markers, {f"g-{i}" for i in range(30)})
        self.assertEqual(self.saved, {})
        token = self.runtime.acquire("after-completion")
        self.runtime.release(token)

    def test_guest_followups_count_once_and_two_guests_count_twice(self):
        expected = set()
        for ident in ("guest-one", "guest-two"):
            client = self.client(ident)
            for question in ("first question", "follow up"):
                with self.subTest(ident=ident, question=question):
                    with client.post("/chat/get", data={"msg": question}) as response:
                        self.assertEqual(response.status_code, 200)
                        text = response.get_data(as_text=True)
                    self.assertIn('"type": "done"', text)
                    self.assertNotIn('"type": "error"', text)
                    self.assertIn('"conv_id": null', text)
                    self.assertIn('"new_conversation_created": false', text)
                    expected.add(ident)
                    self.assertEqual(self.guest_markers, expected)
            self.assertEqual(len(self.runtime.load_guest(ident)), 4)
            self.assertEqual(self.seen[-1]["chat_history"][0]["content"], "first question")
            self.assertIsNone(self.seen[-1]["uid"])
            self.assertIsNone(self.seen[-1]["user_email"])
        self.assertEqual(self.saved, {})

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
        self.assertEqual(self.guest_markers, set())

    def test_topic_events_follow_saved_reply_ids_for_both_roles_and_resubmissions(self):
        expected, markers = {}, set()
        for guest in (True, False):
            ident = "topic-guest" if guest else "topic-account"
            client = self.client(ident, guest=guest)
            prior_history = []
            calls = Mock()
            save = Mock(wraps=self.runtime.save_guest if guest else self.ns["upsert_conversation"])
            calls.attach_mock(save, "save")
            calls.attach_mock(self.ns["classify_topic"], "classify")
            calls.attach_mock(self.ns["record_topic_usage"], "record")
            persistence = (patch.object(self.runtime, "save_guest", save) if guest
                           else patch.dict(self.ns, {"upsert_conversation": save}))
            with persistence:
                for question in ("How do I enroll?", "And the requirements?", "How do I enroll?"):
                    with self.subTest(guest=guest, question=question):
                        with client.post("/chat/get", data={
                            "msg": question, "topic": "forged-topic", "msg_id": "forged-id",
                            "event_id": "forged-id", "conv_id": "forged-conversation",
                            "session_id": "forged-session", "history": "forged-history",
                            "has_image": "true",
                        }) as response:
                            self.assertEqual(response.status_code, 200)
                            done = self.done_event(response.get_data(as_text=True))
                        history = (self.runtime.load_guest(ident) if guest
                                   else self.saved[(ident, done["conv_id"])])
                        self.assertEqual(history[:-2], prior_history)
                        self.assertEqual(history[-1]["msg_id"], done["msg_id"])
                        self.assertRegex(done["msg_id"], r"^msg-[0-9a-f]{16}$")
                        self.assertNotIn(done["msg_id"], expected)
                        self.ns["classify_topic"].assert_called_with(
                            question, history=prior_history, has_image=False,
                        )
                        self.ns["record_topic_usage"].assert_called_with("enrollment", done["msg_id"])
                        expected[done["msg_id"]] = "enrollment"
                        self.assertEqual(self.topic_records, expected)
                        if guest:
                            markers.add(ident)
                            self.assertIsNone(done["conv_id"])
                        else:
                            self.assertNotEqual(done["conv_id"], "forged-conversation")
                        self.assertEqual(self.guest_markers, markers)
                        prior_history = copy.deepcopy(history)
            self.assertEqual([call[0] for call in calls.mock_calls], ["save", "classify", "record"] * 3)
        self.assertEqual(len(self.topic_records), 6)

    def test_none_classification_skips_topic_usage_for_both_roles(self):
        self.ns["classify_topic"].return_value = None
        for guest in (True, False):
            with self.subTest(guest=guest):
                ident = "small-talk-guest" if guest else "small-talk-account"
                status, text = self.ask(ident, "hello", guest=guest)
                self.assertEqual(status, 200)
                done = self.done_event(text)
                history = (self.runtime.load_guest(ident) if guest
                           else self.saved[(ident, done["conv_id"])])
                self.assertEqual(history[-1]["msg_id"], done["msg_id"])
                self.ns["classify_topic"].assert_called_with("hello", history=[], has_image=False)
                self.ns["record_topic_usage"].assert_not_called()
                self.assertEqual(self.topic_records, {})
                self.assertEqual(self.guest_markers, {"small-talk-guest"})

    def test_topic_failures_keep_done_history_and_markers_with_static_warning(self):
        warnings, expected = [], {}
        self.ns["print"] = warnings.append
        for guest in (True, False):
            ident = "private-guest" if guest else "private-account"
            client = self.client(ident, guest=guest)
            with client.post("/chat/get", data={"msg": "private prior question"}) as response:
                first = self.done_event(response.get_data(as_text=True))
            expected[first["msg_id"]] = "enrollment"
            for index, (name, error) in enumerate((
                ("classify_topic", ValueError("private classifier details")),
                ("record_topic_usage", RuntimeError("private writer details")),
            )):
                with self.subTest(guest=guest, failure=name):
                    failed = Mock(side_effect=error)
                    with (
                        patch.dict(self.ns, {name: failed}),
                        client.post("/chat/get", data={"msg": "private follow up"}) as response,
                    ):
                        self.assertEqual(response.status_code, 200)
                        text = response.get_data(as_text=True)
                    done = self.done_event(text)
                    history = (self.runtime.load_guest(ident) if guest
                               else self.saved[(ident, done["conv_id"])])
                    self.assertEqual(history[-1]["msg_id"], done["msg_id"])
                    self.assertEqual(history[-1]["content"], "Answer: private follow up")
                    self.assertEqual(len(history), 2 * (index + 2))
                    failed.assert_called_once()
                    self.assertEqual(self.topic_records, expected)
                    self.assertEqual(self.guest_markers, {"private-guest"})
            with client.post("/chat/get", data={"msg": "accounting recovered"}) as response:
                done = self.done_event(response.get_data(as_text=True))
            expected[done["msg_id"]] = "enrollment"
            self.assertEqual(self.topic_records, expected)
        self.assertEqual(warnings, ["[chat] Topic usage could not be recorded (non-fatal)."] * 4)

    def test_image_only_turn_passes_context_and_image_flag_only_to_classifier(self):
        expected = {}
        for guest in (True, False):
            with self.subTest(guest=guest):
                ident = "image-guest" if guest else "image-account"
                client = self.client(ident, guest=guest)
                with client.post("/chat/get", data={"msg": "How do I enroll?"}) as response:
                    first = self.done_event(response.get_data(as_text=True))
                expected[first["msg_id"]] = "enrollment"
                prior_history = copy.deepcopy(self.runtime.load_guest(ident) if guest
                                              else self.saved[(ident, first["conv_id"])])
                image = io.BytesIO()
                with Image.new("RGB", (2, 2), color="white") as pixels:
                    pixels.save(image, format="PNG")
                image.seek(0)
                self.ns["classify_topic"].reset_mock()
                self.ns["record_topic_usage"].reset_mock()
                self.ns["encode_image"].reset_mock()
                with client.post("/chat/get", data={"image": (image, "question.png")}) as response:
                    self.assertEqual(response.status_code, 200)
                    done = self.done_event(response.get_data(as_text=True))
                self.ns["encode_image"].assert_called_once()
                self.ns["classify_topic"].assert_called_once_with("", history=prior_history, has_image=True)
                self.ns["record_topic_usage"].assert_called_once_with("enrollment", done["msg_id"])
                self.assertEqual(self.seen[-1]["image_data"], "test-encoded-image")
                history = (self.runtime.load_guest(ident) if guest
                           else self.saved[(ident, done["conv_id"])])
                self.assertEqual(history[-2]["content"], " [Image Uploaded]")
                self.assertEqual(history[-1]["msg_id"], done["msg_id"])
                expected[done["msg_id"]] = "enrollment"
                self.assertEqual(self.topic_records, expected)
                self.assertEqual(self.guest_markers, {"image-guest"})

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
        self.assertEqual(self.guest_markers, set())
        self.assertEqual(self.topic_records, {})
        self.ns["classify_topic"].assert_not_called()

    def test_failed_guest_turn_then_retry_counts_only_success(self):
        client = self.client("guest-retry")

        def stream(messages):
            yield "unfinished answer"
            raise RuntimeError("simulated provider disconnect")

        with (
            patch.dict(self.ns, {"stream_answer": stream}),
            client.post("/chat/get", data={"msg": "failed question"}) as response,
        ):
            text = response.get_data(as_text=True)
        self.assertIn('"type": "error"', text)
        self.assertNotIn('"type": "done"', text)
        self.assertEqual(self.guest_markers, set())
        self.assertEqual(self.runtime.load_guest("guest-retry"), [])
        self.assertEqual(self.topic_records, {})
        self.ns["classify_topic"].assert_not_called()

        with client.post("/chat/get", data={"msg": "retry question"}) as response:
            text = response.get_data(as_text=True)
        self.assertIn('"type": "done"', text)
        self.assertNotIn('"type": "error"', text)
        self.assertEqual(self.guest_markers, {"guest-retry"})
        self.assertEqual(self.seen[-1]["chat_history"], [])
        self.assertEqual(
            [message["content"] for message in self.runtime.load_guest("guest-retry")],
            ["retry question", "Answer: retry question"],
        )
        self.assertEqual(self.saved, {})

    def test_empty_guest_answers_do_not_count(self):
        for index, chunks in enumerate(((), ("", " \t\r\n"))):
            with self.subTest(chunks=chunks):
                self.ns["stream_answer"] = lambda messages, chunks=chunks: iter(chunks)
                ident = f"empty-answer-{index}"
                status, text = self.ask(ident)
                self.assertEqual(status, 200)
                self.assertIn('"type": "error"', text)
                self.assertNotIn('"type": "done"', text)
                self.assertEqual(self.runtime.load_guest(ident), [])
                self.assertEqual(self.guest_markers, set())
                self.assertEqual(self.topic_records, {})
                self.ns["classify_topic"].assert_not_called()
        self.assertEqual(self.saved, {})

    def test_guest_preparation_failures_do_not_count(self):
        results = [RuntimeError("preparation failed"), None, {}, {"messages_to_llm": []}]
        with patch.object(self.ns["app_graph"], "invoke", side_effect=results):
            for index in range(len(results)):
                with self.subTest(index=index):
                    ident = f"preparation-failed-{index}"
                    status, text = self.ask(ident)
                    self.assertEqual(status, 200)
                    self.assertIn('"type": "error"', text)
                    self.assertNotIn('"type": "done"', text)
                    self.assertEqual(self.runtime.load_guest(ident), [])
                    self.assertEqual(self.guest_markers, set())
                    self.assertEqual(self.topic_records, {})
                    self.ns["classify_topic"].assert_not_called()
        self.assertEqual(self.saved, {})

    def test_worker_capacity_failures_do_not_count(self):
        for name, pool in (("answer", self.pool), ("persistence", self.preparation)):
            with self.subTest(pool=name):
                with patch.object(pool, "submit", side_effect=CapacityError("test capacity")):
                    ident = f"busy-{name}"
                    status, text = self.ask(ident)
                self.assertEqual(status, 200)
                self.assertIn('"type": "error"', text)
                self.assertNotIn('"type": "done"', text)
                self.assertEqual(self.runtime.load_guest(ident), [])
                self.assertEqual(self.guest_markers, set())
                self.assertEqual(self.topic_records, {})
                self.ns["classify_topic"].assert_not_called()
        self.assertEqual(self.saved, {})

    def test_rejected_requests_do_not_count(self):
        cases = (
            ("anonymous", self.app.test_client(), {"msg": "hello"}, 401),
            ("blank", self.client("blank-request"), {"msg": " \t\n"}, 400),
            ("long", self.client("long-request"), {"msg": "x" * 4001}, 400),
            ("account-blank", self.client("blank-account", guest=False), {"msg": " "}, 400),
            ("account-long", self.client("long-account", guest=False), {"msg": "x" * 4001}, 400),
            ("invalid-image", self.client("invalid-image"),
             {"image": (io.BytesIO(b"not an image"), "question.png")}, 400),
        )
        for name, client, data, status in cases:
            with self.subTest(request=name):
                with client.post("/chat/get", data=data) as response:
                    self.assertEqual(response.status_code, status)
                    self.assertNotIn('"type": "done"', response.get_data(as_text=True))
                self.assertEqual(self.guest_markers, set())
                self.assertEqual(self.topic_records, {})
                self.ns["classify_topic"].assert_not_called()
        self.assertEqual(self.seen, [])
        self.assertEqual(self.saved, {})

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
        self.assertEqual(self.guest_markers, set())
        self.assertEqual(self.topic_records, {})
        self.ns["classify_topic"].assert_not_called()

    def test_topic_usage_checks_budget_after_save_marker_and_classification(self):
        markers = set()
        for guest in (True, False):
            steps = ("save", "marker", "classify") if guest else ("save", "classify")
            for step in steps:
                with self.subTest(guest=guest, step=step):
                    ident = f"expired-{guest}-{step}"
                    budget = RequestBudget()
                    classifier = self.ns["classify_topic"]
                    classifier.reset_mock()
                    name = {"save": "upsert_conversation", "marker": "record_guest_conversation",
                            "classify": "classify_topic"}[step]
                    operation = (self.runtime.save_guest if guest and step == "save"
                                 else self.ns[name])

                    def expire_after(*args, operation=operation, budget=budget, **kwargs):
                        result = operation(*args, **kwargs)
                        budget.deadline = 0
                        return result

                    boundary = (patch.object(self.runtime, "save_guest", expire_after)
                                if guest and step == "save" else patch.dict(self.ns, {name: expire_after}))
                    with (
                        patch.dict(self.ns, {"RequestBudget": lambda budget=budget: budget, "print": Mock()}),
                        boundary,
                    ):
                        status, text = self.ask(ident, guest=guest)
                    self.assertEqual(status, 200)
                    self.assertIn('"type": "error"', text)
                    self.assertNotIn('"type": "done"', text)
                    self.assertEqual(self.topic_records, {})
                    self.ns["record_topic_usage"].assert_not_called()
                    self.assertEqual(classifier.call_count, int(step == "classify"))
                    history = (self.runtime.load_guest(ident) if guest else next(
                        history for (uid, _), history in self.saved.items() if uid == ident
                    ))
                    self.assertEqual(len(history), 2)
                    if guest and step != "save":
                        markers.add(ident)
                    self.assertEqual(self.guest_markers, markers)

    def test_close_before_pipeline_start_releases_admission(self):
        response = self.client("unread").post("/chat/get", data={"msg": "hello"}, buffered=False)
        response.close()
        self.assertEqual(self.seen, [])
        self.assertEqual(self.runtime.load_guest("unread"), [])
        self.assertEqual(self.guest_markers, set())
        self.assertEqual(self.topic_records, {})
        self.ns["classify_topic"].assert_not_called()
        token = self.runtime.acquire("session:unread")
        self.runtime.release(token)

    def test_closed_guest_stream_does_not_count_a_late_answer(self):
        release, started = threading.Event(), threading.Event()

        def stream(messages):
            yield "partial text"
            started.set()
            release.wait(8)
            yield "late text"

        self.ns["stream_answer"] = stream
        response = self.client("closed-guest").post(
            "/chat/get", data={"msg": "hello"}, buffered=False,
        )
        text = ""
        try:
            for chunk in response.response:
                text += chunk.decode("utf-8")
                if '"type": "chunk"' in text:
                    break
            self.assertIn("partial text", text)
            self.assertTrue(started.wait(2))
            self.assertEqual(self.guest_markers, set())
            self.assertEqual(self.topic_records, {})
        finally:
            response.close()
            release.set()
            self.pool.shutdown()
        self.assertNotIn('"type": "done"', text)
        self.assertEqual(self.runtime.load_guest("closed-guest"), [])
        self.assertEqual(self.guest_markers, set())
        self.assertEqual(self.saved, {})
        self.assertEqual(self.topic_records, {})
        self.ns["classify_topic"].assert_not_called()

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
            self.assertEqual(self.topic_records, {})
            self.ns["classify_topic"].assert_not_called()
        finally:
            first.close()
        with first_client.post("/chat/get", data={"msg": "retry"}) as response:
            text = response.get_data(as_text=True)
            self.assertIn('"type": "done"', text)
            self.assertIn('"new_conversation_created": true', text)
        self.assertEqual(self.guest_markers, set())
        done = self.done_event(text)
        self.assertEqual(self.topic_records, {done["msg_id"]: "enrollment"})

    def test_first_guest_cookie_is_set_before_streaming_and_cannot_bypass_lock(self):
        first_client = self.app.test_client()
        with first_client.session_transaction() as sess:
            sess.update(user="guest", is_guest=True, role="guest")
            self.assertNotIn("session_id", sess)
        first = first_client.post("/chat/get", data={"msg": "first"}, buffered=False)
        try:
            self.assertEqual(first.status_code, 200)
            with first_client.session_transaction() as sess:
                session_id = sess["session_id"]
            self.assertTrue(session_id)
            cookie = first.headers["Set-Cookie"].split(";", 1)[0]
            self.assertTrue(cookie.startswith(self.app.config["SESSION_COOKIE_NAME"] + "="))
            self.assertEqual(self.seen, [])
            self.assertEqual(self.guest_markers, set())
            self.assertEqual(self.topic_records, {})

            copied_client = self.app.test_client(use_cookies=False)
            with (
                patch.dict(self.ns, {"chat_runtime": ChatRuntime(self.runtime.path)}),
                copied_client.post(
                    "/chat/get", data={"msg": "overlapping"}, headers={"Cookie": cookie},
                ) as second,
            ):
                self.assertEqual(second.status_code, 409)
            self.assertEqual(self.runtime.load_guest(session_id), [])
            self.assertEqual(self.guest_markers, set())
            self.assertEqual(self.topic_records, {})
            self.assertIn('"type": "done"', first.get_data(as_text=True))
        finally:
            first.close()
        self.assertEqual(self.guest_markers, {session_id})

        # Replay the pre-completion cookie, not any state updated after tracking.
        with copied_client.post(
            "/chat/get", data={"msg": "follow up"}, headers={"Cookie": cookie},
        ) as response:
            self.assertIn('"type": "done"', response.get_data(as_text=True))
        self.assertEqual(self.guest_markers, {session_id})
        self.assertEqual(len(self.runtime.load_guest(session_id)), 4)
        self.assertEqual(self.saved, {})
        self.assertEqual(len(self.topic_records), 2)

    def test_account_without_uid_cannot_report_unsaved_success(self):
        client = self.client("invalid-account", guest=False)
        with client.session_transaction() as sess:
            sess.pop("uid")
        response = client.post("/chat/get", data={"msg": "hello"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.saved, {})
        self.assertEqual(self.guest_markers, set())
        self.assertEqual(self.topic_records, {})
        self.ns["classify_topic"].assert_not_called()

    def test_storage_failure_cannot_report_success(self):
        with patch.object(self.runtime, "save_guest", side_effect=RuntimeError("disk full")):
            _, text = self.ask("save-failed")
        self.assertIn('"type": "error"', text)
        self.assertNotIn('"type": "done"', text)
        self.assertEqual(self.runtime.load_guest("save-failed"), [])
        self.assertEqual(self.guest_markers, set())
        with patch.dict(self.ns, {"upsert_conversation": Mock(side_effect=RuntimeError("disk full"))}):
            _, text = self.ask("account-save-failed", guest=False)
        self.assertIn('"type": "error"', text)
        self.assertNotIn('"type": "done"', text)
        self.assertEqual(self.saved, {})
        self.assertEqual(self.guest_markers, set())
        self.assertEqual(self.topic_records, {})
        self.ns["classify_topic"].assert_not_called()
        self.ns["record_topic_usage"].assert_not_called()

    def test_failed_metric_writer_keeps_done_and_next_turn_can_record(self):
        ident = "metric-guest"
        client = self.client(ident)
        warnings = []
        self.ns["print"] = warnings.append
        details = ("private marker failure one", "private marker failure two")
        questions = ("metric question one", "metric question two")
        writer = Mock(side_effect=[RuntimeError(details[0]), ValueError(details[1])])
        topic_ids = set()
        with patch.dict(self.ns, {"record_guest_conversation": writer}):
            for index, question in enumerate(questions):
                with client.post("/chat/get", data={"msg": question}) as response:
                    self.assertEqual(response.status_code, 200)
                    text = response.get_data(as_text=True)
                self.assertIn(f"Answer: {question}", text)
                self.assertIn('"type": "done"', text)
                self.assertNotIn('"type": "error"', text)
                self.assertEqual(self.guest_markers, set())
                self.assertEqual(len(self.runtime.load_guest(ident)), 2 * (index + 1))
                topic_ids.add(self.done_event(text)["msg_id"])
                self.assertEqual(self.topic_records, dict.fromkeys(topic_ids, "enrollment"))
        self.assertEqual(len(warnings), 2)
        self.assertTrue(warnings[0].strip())
        self.assertEqual(warnings[0], warnings[1])
        for private_text in (ident, *details, *questions):
            self.assertNotIn(private_text, warnings[0])

        with client.post("/chat/get", data={"msg": "tracking recovered"}) as response:
            text = response.get_data(as_text=True)
        self.assertIn('"type": "done"', text)
        self.assertNotIn('"type": "error"', text)
        self.assertEqual(self.guest_markers, {ident})
        self.assertEqual(len(self.seen[-1]["chat_history"]), 4)
        self.assertEqual(len(self.runtime.load_guest(ident)), 6)
        topic_ids.add(self.done_event(text)["msg_id"])
        self.assertEqual(len(topic_ids), 3)
        self.assertEqual(self.topic_records, dict.fromkeys(topic_ids, "enrollment"))
        with client.session_transaction() as sess:
            self.assertEqual(sess["session_id"], ident)
        self.assertEqual(self.saved, {})

    def test_clearing_without_chatting_does_not_count(self):
        client = self.client("unused-guest")
        for _ in range(2):
            with client.post("/chat/clear") as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["status"], "success")
            with client.session_transaction() as sess:
                self.assertNotIn("session_id", sess)
            self.assertEqual(self.guest_markers, set())
            self.assertEqual(self.topic_records, {})
        self.assertEqual(self.runtime.load_guest("unused-guest"), [])
        self.assertEqual(self.seen, [])
        self.assertEqual(self.saved, {})
        self.ns["classify_topic"].assert_not_called()

    def test_new_chat_counts_only_after_a_successful_guest_turn(self):
        client = self.client("before-clear")
        with client.post("/chat/get", data={"msg": "old question"}) as response:
            self.assertIn('"type": "done"', response.get_data(as_text=True))
        self.assertEqual(self.guest_markers, {"before-clear"})
        with client.post("/chat/clear") as response:
            self.assertEqual(response.get_json()["status"], "success")
        with client.session_transaction() as sess:
            self.assertNotIn("session_id", sess)
        self.assertEqual(self.runtime.load_guest("before-clear"), [])
        self.assertEqual(self.guest_markers, {"before-clear"})

        with client.post("/chat/get", data={"msg": "new question"}, buffered=False) as response:
            with client.session_transaction() as sess:
                new_id = sess["session_id"]
            self.assertNotEqual(new_id, "before-clear")
            self.assertEqual(self.guest_markers, {"before-clear"})
            self.assertIn('"type": "done"', response.get_data(as_text=True))
        self.assertEqual(self.guest_markers, {"before-clear", new_id})
        self.assertEqual(self.seen[-1]["chat_history"], [])
        self.assertEqual(
            [message["content"] for message in self.runtime.load_guest(new_id)],
            ["new question", "Answer: new question"],
        )
        self.assertEqual(self.saved, {})

    def test_guest_history_deletion_or_expiry_does_not_duplicate_a_marker(self):
        expected = set()
        for removal in ("deletion", "expiry"):
            with self.subTest(removal=removal):
                ident = f"history-{removal}"
                client = self.client(ident)
                with patch.dict(os.environ, {"GUEST_HISTORY_TTL_SECONDS": "60"}):
                    with client.post("/chat/get", data={"msg": "old question"}) as response:
                        self.assertIn('"type": "done"', response.get_data(as_text=True))
                    expected.add(ident)
                    self.assertEqual(self.guest_markers, expected)
                    if removal == "deletion":
                        self.runtime.delete_guest(ident)
                    now = time.time() + (61 if removal == "expiry" else 0)
                    with patch("rag.chat_runtime.time", SimpleNamespace(time=lambda now=now: now)):
                        self.assertEqual(self.runtime.load_guest(ident), [])
                        with client.post("/chat/get", data={"msg": "after removal"}) as response:
                            self.assertIn('"type": "done"', response.get_data(as_text=True))
                self.assertEqual(self.guest_markers, expected)
                self.assertEqual(self.seen[-1]["chat_history"], [])
                self.assertEqual(len(self.runtime.load_guest(ident)), 2)
                with client.session_transaction() as sess:
                    self.assertEqual(sess["session_id"], ident)
        self.assertEqual(self.saved, {})

    def test_forged_form_conv_id_cannot_choose_the_guest_marker(self):
        expected = set()
        for ident in ("real-guest-one", "real-guest-two"):
            client = self.client(ident)
            for claimed_id in ("forged-shared-id", "real-guest-one"):
                with self.subTest(ident=ident, claimed_id=claimed_id):
                    with client.post(
                        "/chat/get", data={"msg": "hello", "conv_id": claimed_id},
                    ) as response:
                        text = response.get_data(as_text=True)
                    self.assertIn('"type": "done"', text)
                    self.assertIn('"conv_id": null', text)
                    expected.add(ident)
                    self.assertEqual(self.guest_markers, expected)
            self.assertEqual(len(self.runtime.load_guest(ident)), 4)
        self.assertEqual(self.runtime.load_guest("forged-shared-id"), [])
        self.assertEqual(self.saved, {})

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
