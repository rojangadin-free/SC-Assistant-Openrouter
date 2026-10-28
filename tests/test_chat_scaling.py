"""Execute the real Flask chat route with no model, Pinecone or AWS imports."""

import _bootstrap  # noqa: F401
import ast
import datetime
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, Blueprint, Response, jsonify, request, session, stream_with_context
from rag.guest_history import GuestHistory
from rag.progress import ProgressChannel
from rag.runtime import CapacityError, shared_pool, env_int, env_float


class ChatScalingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sc_chat_scaling_")
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"STORE_BACKEND": "file", "GUEST_HISTORY_DIR": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.inputs, self.saves = [], []

        def invoke(payload, **kwargs):
            self.inputs.append(payload)
            return {"messages_to_llm": ["fake compiled prompt"], "citations": [{"source": "directory.txt"}]}

        def session_id():
            session.setdefault("session_id", uuid.uuid4().hex)
            return session["session_id"]

        self.namespace = {
            "bp": Blueprint("chat", __name__, url_prefix="/chat"),
            "request": request, "session": session, "Response": Response,
            "jsonify": jsonify, "stream_with_context": stream_with_context,
            "json": json, "datetime": datetime, "threading": threading, "time": time,
            "uuid": uuid, "os": os, "CapacityError": CapacityError,
            "shared_pool": shared_pool, "env_int": env_int, "env_float": env_float,
            "guest_history": GuestHistory(), "get_session_id": session_id,
            "resolve_role": lambda *a, **k: "student", "ProgressChannel": ProgressChannel,
            "app_graph": SimpleNamespace(invoke=invoke),
            "stream_answer": lambda _: iter(["School ", "answer"]),
            "get_conversation": lambda *a: {"messages": [{"role": "user", "content": "earlier question"}]},
            "upsert_conversation": lambda *a: self.saves.append(a),
            "looks_unanswered": lambda _: False,
        }
        with open("sc_assistant/chat.py", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"chat", "_is_guest"}]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "sc_assistant/chat.py", "exec"), self.namespace)
        app = Flask(__name__)
        app.secret_key = "offline-test-secret"
        app.register_blueprint(self.namespace["bp"])
        self.client = app.test_client()

    def login(self, guest=True):
        with self.client.session_transaction() as state:
            state.update(user="guest" if guest else "student@test.invalid", is_guest=guest,
                         uid=None if guest else "student-id", current_conv_id=None if guest else "conv-id")

    def events(self, response):
        return [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                if line.startswith("data: ")]

    def test_guest_history_survives_new_store_instance_without_graph_checkpoints(self):
        self.login()
        first = self.events(self.client.post("/chat/get", data={"msg": "list the deans"}))
        self.assertEqual(first[-1]["type"], "done")
        self.namespace["guest_history"] = GuestHistory()
        second = self.events(self.client.post("/chat/get", data={"msg": "what about tuition"}))
        self.assertEqual(second[-1]["type"], "done")
        self.assertEqual(len(self.inputs[1]["chat_history"]), 2)
        self.assertEqual(self.inputs[1]["chat_history"][-1]["citations"], [{"source": "directory.txt"}])
        self.assertEqual(self.saves, [])

    def test_authenticated_save_appends_only_the_new_pair(self):
        self.login(False)
        events = self.events(self.client.post("/chat/get", data={"msg": "enrollment requirements"}))
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(len(self.inputs[0]["chat_history"]), 1)
        self.assertEqual(len(self.saves[0][2]), 2)
        self.assertEqual(self.saves[0][2][0]["content"], "enrollment requirements")

    def test_stuck_preparation_returns_error_and_cancels_queued_task(self):
        self.login()
        pending = Future()
        with patch.dict(self.namespace, {
            "shared_pool": lambda _: SimpleNamespace(submit=lambda *a, **k: pending),
            "env_float": lambda *a: 0,
        }):
            events = self.events(self.client.post("/chat/get", data={"msg": "a question"}))
        self.assertEqual(events[-1]["type"], "error")
        self.assertTrue(events[-1]["retryable"])
        self.assertTrue(pending.cancelled())
        self.assertEqual(self.saves, [])

    def test_partial_stream_failure_never_saves_a_completed_turn(self):
        self.login(False)

        def broken(_):
            yield "Partial answer"
            raise RuntimeError("fake provider failure")

        with patch.dict(self.namespace, {"stream_answer": broken}):
            events = self.events(self.client.post("/chat/get", data={"msg": "a question"}))
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.saves, [])
        self.assertTrue(any(event.get("text") == "Partial answer" for event in events))

    def test_authenticated_atomic_append_is_idempotent_on_sdk_retry(self):
        with open("aws/dynamodb.py", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upsert_conversation"]
        saved, identities = [], set()

        class ClientError(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}

        def update(**kwargs):
            self.assertIn("NOT contains", kwargs["ConditionExpression"])
            values = kwargs["ExpressionAttributeValues"]
            if identities.intersection(values[":turn_ids"]):
                raise ClientError()
            identities.update(values[":turn_ids"])
            saved.extend(values[":turn"])

        namespace = {"datetime": datetime, "ClientError": ClientError,
                     "conversations_table": SimpleNamespace(update_item=update)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "aws/dynamodb.py", "exec"), namespace)
        first = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "answer", "msg_id": "one"}]
        second = [{"role": "user", "content": "second"}, {"role": "assistant", "content": "answer", "msg_id": "two"}]
        for turn in (first, second, first):
            namespace["upsert_conversation"]("student", "conversation", turn, "2026-10-28")
        self.assertEqual(saved, first + second)


if __name__ == "__main__":
    unittest.main()
