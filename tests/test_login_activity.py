"""Successful sign-in history: bounded storage and real conditional retries."""

import concurrent.futures
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401

_TMP = tempfile.TemporaryDirectory(prefix="sc_login_activity_test_")
os.environ["STORE_BACKEND"] = "file"
os.environ["LOGIN_ACTIVITY_FILE"] = os.path.join(_TMP.name, "initial.json")

from test_storage_concurrency import ConditionalTable, Conflict

from rag import login_activity, store


class LoginActivityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="sc_login_activity_case_")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "logins.json"
        env = patch.dict(os.environ, {"STORE_BACKEND": "file", "LOGIN_ACTIVITY_FILE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        self.identity = {"uid": "test-sub", "display_name": "Test User", "email": "test@example.invalid"}

    def test_no_invented_history_and_repeated_logins_are_distinct(self):
        self.assertEqual(login_activity._load(), {"events": {}})
        self.assertFalse(self.path.exists())
        first = login_activity.record_login(**self.identity)
        second = login_activity.record_login(**self.identity)
        self.assertNotEqual(first["id"], second["id"])
        # A fresh store handle sees the same file, not process-local state.
        data = login_activity._load()
        self.assertEqual(set(data), {"events", "updated_at"})
        self.assertEqual(data["events"], {first["id"]: first, second["id"]: second})
        for event in data["events"].values():
            self.assertEqual(set(event), {"id", "at", "uid", "display_name", "email"})
            self.assertEqual({key: event[key] for key in self.identity}, self.identity)
            at = datetime.fromisoformat(event["at"])
            self.assertEqual(at.utcoffset(), timedelta(0))
            self.assertLess(abs((datetime.now(timezone.utc) - at).total_seconds()), 10)

    def test_retention_drops_expired_and_oldest_rows(self):
        now = datetime.now(timezone.utc)
        events = {str(i): {"id": str(i), "at": (now - timedelta(minutes=i + 1)).isoformat(), **self.identity}
                  for i in range(login_activity.MAX_EVENTS + 2)}
        events["expired"] = {"id": "expired", "at": (now - timedelta(days=31)).isoformat(), **self.identity}
        events["invalid"] = {"id": "invalid", "at": "not-a-date", **self.identity}
        login_activity._store().save({"events": events})
        newest = login_activity.record_login(**self.identity)
        kept = login_activity._load()["events"]
        self.assertEqual(len(kept), login_activity.MAX_EVENTS)
        self.assertIn(newest["id"], kept)
        self.assertIn("0", kept)
        self.assertNotIn(str(login_activity.MAX_EVENTS - 1), kept)
        self.assertNotIn("expired", kept)
        self.assertNotIn("invalid", kept)

    def test_utf8_and_json_escaping_fit_below_the_dynamodb_payload_guard(self):
        now = datetime.now(timezone.utc)
        identity = {**self.identity, "display_name": '"\n\\\u754c' * 256}
        events = {str(i): {"id": str(i), "at": (now - timedelta(seconds=i + 1)).isoformat(), **identity}
                  for i in range(login_activity.MAX_EVENTS)}
        table = ConditionalTable()
        table.items["login_activity"] = {"store": "login_activity", "payload": json.dumps({"events": events})}
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            newest = login_activity.record_login(**identity)
            data = login_activity._load()
        payload = table.items["login_activity"]["payload"]
        self.assertLessEqual(len(payload.encode("utf-8")), login_activity.MAX_PAYLOAD_BYTES)
        self.assertLess(len(payload.encode("utf-8")), 390 * 1024)
        self.assertLess(len(data["events"]), login_activity.MAX_EVENTS)
        self.assertGreater(len(data["events"]), 1)
        self.assertIn(newest["id"], data["events"])
        self.assertNotIn(str(login_activity.MAX_EVENTS - 1), data["events"])
        self.assertEqual(data["events"][newest["id"]]["display_name"], identity["display_name"])

    def test_same_event_id_and_timestamp_are_reused_after_a_conditional_conflict(self):
        attempted = []

        class RetryOnce(ConditionalTable):
            def put_item(self, Item, **kwargs):
                attempted.append(json.loads(Item["payload"])["events"])
                if len(attempted) == 1:
                    raise Conflict()
                return super().put_item(Item=Item, **kwargs)

        table = RetryOnce()
        now = datetime.now(timezone.utc)
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table), \
                patch.object(login_activity, "uuid4", return_value=SimpleNamespace(hex="one-event")) as uuid, \
                patch.object(login_activity, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            event = login_activity.record_login(**self.identity)
            kept = login_activity._load()["events"]
        uuid.assert_called_once_with()
        clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(len(attempted), 2)
        self.assertEqual(attempted[0], attempted[1])
        self.assertEqual(kept, {"one-event": event})

    def test_concurrent_signins_survive_conditional_retries(self):
        table = ConditionalTable()
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                events = list(pool.map(lambda _: login_activity.record_login(**self.identity), range(30)))
            kept = login_activity._load()["events"]
        self.assertEqual(len(events), 30)
        self.assertEqual(len({event["id"] for event in events}), 30)
        self.assertEqual(kept, {event["id"]: event for event in events})
        self.assertGreater(table.conflicts, 0)

    def test_failed_dynamodb_reads_do_not_overwrite_history(self):
        table = ConditionalTable()
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            login_activity.record_login(**self.identity)
            before = copy.deepcopy(table.items)
            writes = table.writes
            table.fail_read = True
            with self.assertRaises(RuntimeError):
                login_activity.record_login(**self.identity)
        self.assertEqual(table.items, before)
        self.assertEqual(table.writes, writes)

    def test_corrupt_files_are_not_replaced_by_an_empty_history(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError), patch("builtins.print") as output:
            login_activity.record_login(**self.identity)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")
        output.assert_not_called()

    def test_incomplete_or_oversized_identity_cannot_persist_arbitrary_data(self):
        for invalid in ({"uid": None}, {"email": ""}, {"display_name": {"IdToken": "SECRET"}},
                        {"display_name": "x" * login_activity.MAX_PAYLOAD_BYTES}):
            with self.subTest(fields=tuple(invalid)), self.assertRaises(ValueError):
                login_activity.record_login(**{**self.identity, **invalid})
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
