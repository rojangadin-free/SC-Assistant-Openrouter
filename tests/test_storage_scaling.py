"""Deterministic CAS, guest expiry and multiprocess file tests; no AWS traffic."""

import _bootstrap  # noqa: F401
import copy
import importlib.util
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from rag import store
from rag.guest_history import GuestHistory


class Conflict(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Table:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        self.fail_read = False

    def get_item(self, Key, ConsistentRead=False):
        if not ConsistentRead:
            raise AssertionError("Mutation reads must be consistent")
        if self.fail_read:
            raise RuntimeError("simulated read failure")
        with self.lock:
            return {"Item": copy.deepcopy(self.items[Key["store"]])} if Key["store"] in self.items else {}

    def put_item(self, Item, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues=None):
        with self.lock:
            current = self.items.get(Item["store"], {})
            if ExpressionAttributeValues is None:
                if "version" in current:
                    raise Conflict()
            elif current.get("version") != ExpressionAttributeValues[":expected"]:
                raise Conflict()
            self.items[Item["store"]] = copy.deepcopy(Item)


def increment_file(path, count):
    os.environ["STORE_BACKEND"] = "file"
    instance = store.JsonBlobStore("counter", path, lambda: {"count": 0})
    for _ in range(count):
        instance.mutate(lambda data: data.update(count=data["count"] + 1))


class StorageScalingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sc_scaling_")
        self.addCleanup(self.temp.cleanup)
        self.table = Table()
        self.environment = patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        mocked = patch.object(store, "_table", return_value=self.table)
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_independent_writers_replay_and_keep_both_changes(self):
        barrier = threading.Barrier(2)
        attempts = {"a": 0, "b": 0}

        def update(key):
            instance = store.JsonBlobStore("shared", "unused", lambda: {"rows": {}})

            def change(data):
                attempts[key] += 1
                if attempts[key] == 1:
                    barrier.wait(timeout=3)
                data["rows"][key] = key
            instance.mutate(change)

        with ThreadPoolExecutor(2) as pool:
            list(pool.map(update, ("a", "b")))
        self.assertGreater(sum(attempts.values()), 2)
        self.assertEqual(json.loads(self.table.items["shared"]["payload"])["rows"], {"a": "a", "b": "b"})

    def test_legacy_payload_is_versioned_without_losing_data(self):
        self.table.items["old"] = {"store": "old", "payload": '{"rows":{"existing":1}}'}
        instance = store.JsonBlobStore("old", "unused", lambda: {"rows": {}})
        instance.mutate(lambda d: d["rows"].update(new=2))
        self.assertEqual(instance.load()["rows"], {"existing": 1, "new": 2})
        self.assertEqual(self.table.items["old"]["version"], 1)

    def test_failed_read_and_oversize_never_write(self):
        instance = store.JsonBlobStore("safe", "unused", lambda: {"rows": {}})
        instance.save({"rows": {"keep": True}})
        before = copy.deepcopy(self.table.items)
        self.table.fail_read = True
        with self.assertRaises(RuntimeError):
            instance.mutate(lambda d: d["rows"].clear())
        self.assertEqual(self.table.items, before)
        self.table.fail_read = False
        with self.assertRaises(ValueError):
            instance.mutate(lambda d: d.update(huge="x" * (400 * 1024)))
        self.assertEqual(self.table.items, before)

    def test_guest_histories_are_shared_isolated_and_expire(self):
        one, two = GuestHistory(), GuestHistory()
        turn = [{"role": "user", "content": "list deans"}, {"role": "assistant", "content": "directory"}]
        with patch("rag.guest_history.time.time", return_value=100):
            one.append("guest-a", turn)
            self.assertEqual(two.load("guest-a"), turn)
            self.assertEqual(two.load("guest-b"), [])
        with patch("rag.guest_history.time.time", return_value=4000):
            self.assertEqual(two.load("guest-a"), [])
        self.assertTrue(all("guest-a" not in key for key in self.table.items))
        self.assertTrue(all(isinstance(item.get("expires_at"), int) for item in self.table.items.values()))

    def test_30_votes_from_independent_worker_modules_keep_aggregates(self):
        workers = []
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "rag", "feedback.py")
        for index in range(6):
            spec = importlib.util.spec_from_file_location("feedback_worker_" + str(index), path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            workers.append(module)

        def vote(index):
            result = workers[index % len(workers)].record_vote(
                "msg-" + str(index), "down", question="what are the tuition fees?"
            )
            self.assertIsNotNone(result)

        with ThreadPoolExecutor(30) as pool:
            list(pool.map(vote, range(30)))
        self.assertEqual(workers[0].feedback_stats()["down"], 30)
        self.assertEqual(len(workers[0]._load()["votes"]), 30)

    def test_independent_file_processes_do_not_lose_increments(self):
        path = os.path.join(self.temp.name, "counter.json")
        with patch.dict(os.environ, {"STORE_BACKEND": "file"}):
            context = multiprocessing.get_context("spawn")
            processes = [context.Process(target=increment_file, args=(path, 10)) for _ in range(4)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(15)
                if process.is_alive():
                    process.terminate()
                    process.join()
                self.assertEqual(process.exitcode, 0)
            instance = store.JsonBlobStore("counter", path, lambda: {"count": 0})
            self.assertEqual(instance.load()["count"], 40)


if __name__ == "__main__":
    unittest.main()
