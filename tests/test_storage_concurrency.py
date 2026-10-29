"""Real file-process concurrency and conditional-write DynamoDB contract tests."""

import concurrent.futures
import copy
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

from rag import escalation, feedback, gaps, store


class Conflict(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        super().__init__("conditional conflict")


class ConditionalTable:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        self.writes = 0
        self.conflicts = 0
        self.fail_read = False

    def get_item(self, Key, ConsistentRead=False):
        if not ConsistentRead:
            raise AssertionError("Mutations need consistent reads")
        if self.fail_read:
            raise RuntimeError("simulated read failure")
        with self.lock:
            item = copy.deepcopy(self.items.get(Key["store"]))
        time.sleep(0.001)
        return {"Item": item} if item is not None else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None,
                 ExpressionAttributeNames=None):
        values = ExpressionAttributeValues or {}
        with self.lock:
            old = self.items.get(Item["store"])
            valid = True
            if ConditionExpression == "attribute_not_exists(#pk)":
                valid = old is None
            elif ConditionExpression == "#rev = :rev":
                valid = old is not None and old.get("revision") == values[":rev"]
            elif ConditionExpression:
                valid = old is not None and "revision" not in old and old["payload"] == values[":payload"]
            if not valid:
                self.conflicts += 1
                raise Conflict()
            self.items[Item["store"]] = copy.deepcopy(Item)
            self.writes += 1


def file_increment(args):
    path, number = args
    os.environ["STORE_BACKEND"] = "file"
    document = store.JsonBlobStore("parallel", path, lambda: {"count": 0, "rows": {}})

    def change(data):
        data["count"] += 1
        data["rows"][str(number)] = True

    document.mutate(change)


class StorageConcurrencyTests(unittest.TestCase):
    def test_thirty_file_writes_across_three_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "parallel.json")
            with multiprocessing.get_context("spawn").Pool(3) as pool:
                pool.map(file_increment, [(path, i) for i in range(30)])
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(data["count"], 30)
            self.assertEqual(len(data["rows"]), 30)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_thirty_distinct_votes_and_counters_survive_conflicts(self):
        table = ConditionalTable()
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
                votes = list(pool.map(lambda i: feedback.record_vote(
                    f"test-{i}", "up" if i % 2 else "down", question="library hours",
                ), range(30)))
            self.assertTrue(all(v is not None for v in votes))
            self.assertEqual(feedback.feedback_stats()["total_votes"], 30)
            self.assertEqual(feedback.feedback_stats()["up"], 15)
            with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
                recorded = list(pool.map(lambda _: gaps.record_gap("campus shuttle service"), range(30)))
            self.assertTrue(all(g is not None for g in recorded))
            self.assertEqual(gaps.gap_stats()["total_questions"], 30)
            self.assertGreater(table.conflicts, 0)

    def test_concurrent_escalations_keep_distinct_rows(self):
        table = ConditionalTable()
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
                results = list(pool.map(lambda i: escalation.create_escalation(
                    "campus shuttle", student_email=f"test-{i}@example.invalid",
                ), range(30)))
            self.assertEqual(len({r["id"] for r in results if r}), 30)
            self.assertEqual(escalation.escalation_stats()["total"], 30)

    def test_failed_reads_never_overwrite_existing_data(self):
        table = ConditionalTable()
        doc = store.JsonBlobStore("demo", "unused", lambda: {"rows": {}})
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            doc.save({"rows": {"keep": 1}})
            before = copy.deepcopy(table.items)
            writes = table.writes
            table.fail_read = True
            with self.assertRaises(RuntimeError):
                doc.mutate(lambda d: d["rows"].update({"new": 2}))
            self.assertEqual(table.items, before)
            self.assertEqual(table.writes, writes)

    def test_legacy_payload_upgrades_and_noops_do_not_write(self):
        table = ConditionalTable()
        table.items["demo"] = {"store": "demo", "payload": json.dumps({"rows": {"keep": 1}})}
        doc = store.JsonBlobStore("demo", "unused", lambda: {"rows": {}})
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            doc.mutate(lambda d: d["rows"].update({"new": 2}))
            self.assertIn("revision", table.items["demo"])
            self.assertEqual(doc.load()["rows"], {"keep": 1, "new": 2})
            before = table.writes
            self.assertFalse(doc.mutate(lambda _: False, return_result=True))
            doc.mutate(lambda _: None)
            self.assertEqual(table.writes, before)

    def test_corrupt_files_fail_closed_for_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STORE_BACKEND": "file"}):
            path = Path(tmp) / "broken.json"
            path.write_text("{invalid", encoding="utf-8")
            doc = store.JsonBlobStore("broken", str(path), dict)
            with self.assertRaises(ValueError):
                doc.mutate(lambda d: d.update({"lost": True}))
            self.assertEqual(path.read_text(encoding="utf-8"), "{invalid")


if __name__ == "__main__":
    unittest.main()
