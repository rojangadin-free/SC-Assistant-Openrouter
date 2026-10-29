"""Offline tests for resource limits, query-vector reuse, and prompt costs."""

import ast
import concurrent.futures
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

from rag import freshness
from rag.runtime import BoundedExecutor, CapacityError, RequestBudget
from src.prompt import system_prompt

# Only the LangChain base class is replaced; test the real wrapper methods.
tree = ast.parse(Path("rag/query_embeddings.py").read_text(encoding="utf-8"))
body = [n for n in tree.body if not (
    isinstance(n, ast.ImportFrom) and n.module == "langchain_core.embeddings"
)]
namespace = {"Embeddings": object}
exec(compile(ast.Module(body=body, type_ignores=[]), "rag/query_embeddings.py", "exec"), namespace)  # noqa: S102
CachedQueryEmbeddings = namespace["CachedQueryEmbeddings"]


class RuntimeTests(unittest.TestCase):
    def test_running_and_queued_slots_are_bounded_and_returned(self):
        pool = BoundedExecutor(1, 1, "test")
        release = threading.Event()
        entered = threading.Event()

        def blocked():
            entered.set()
            release.wait(2)

        try:
            first = pool.submit(blocked)
            self.assertTrue(entered.wait(1))
            queued = pool.submit(lambda: None)
            with self.assertRaises(CapacityError):
                pool.submit(lambda: None)
            self.assertTrue(queued.cancel())
            # The cancelled entry still occupies the native queue while the
            # worker is blocked. Repeated cancellation cannot retain unbounded payloads.
            for _ in range(100):
                with self.assertRaises(CapacityError):
                    pool.submit(lambda: 42)
            self.assertEqual(pool._pool._work_queue.qsize(), 1)
            release.set()
            first.result(timeout=2)
            replacement = pool.submit(lambda: 42)
            self.assertEqual(replacement.result(timeout=2), 42)
        finally:
            release.set()
            pool.shutdown()

    def test_failures_do_not_leak_capacity(self):
        pool = BoundedExecutor(1, 0, "test")
        try:
            for _ in range(4):
                future = pool.submit(lambda: 1 / 0)
                with self.assertRaises(ZeroDivisionError):
                    future.result(timeout=2)
            self.assertEqual(pool.submit(lambda: 7).result(timeout=2), 7)
        finally:
            pool.shutdown()

    def test_shared_retrieval_pool_preserves_order_and_limits_work(self):
        pool = BoundedExecutor(4, 24, "test")
        lock = threading.Lock()
        active = peak = 0

        def work(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.003 * (3 - value % 3))
                return value * 2
            finally:
                with lock:
                    active -= 1

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as callers:
                outputs = list(callers.map(
                    lambda _: pool.map_ordered(work, range(12)), range(10),
                ))
            self.assertTrue(all(o == list(range(0, 24, 2)) for o in outputs))
            self.assertLessEqual(peak, 4)
        finally:
            pool.shutdown()

    def test_map_timeout_keeps_running_work_accounted_for(self):
        pool = BoundedExecutor(1, 0, "test")
        release = threading.Event()
        try:
            with self.assertRaises(TimeoutError):
                pool.map_ordered(lambda _: release.wait(2), [1], timeout=0.02)
            with self.assertRaises(CapacityError):
                pool.submit(lambda: None)
        finally:
            release.set()
            pool.shutdown()

    def test_request_deadline_and_cancellation(self):
        with self.assertRaises(TimeoutError):
            RequestBudget(-1).remaining()
        budget = RequestBudget(10)
        self.assertLessEqual(budget.remaining(2), 2)
        budget.cancelled.set()
        with self.assertRaises(TimeoutError):
            budget.remaining()

    def test_query_cache_single_flight_copy_and_eviction(self):
        class Model:
            calls = 0

            def embed_query(self, text):
                self.calls += 1
                time.sleep(0.01)
                return [float(len(text)), 1.0]

            def embed_documents(self, texts):
                return [[len(t)] for t in texts]

        model = Model()
        cache = CachedQueryEmbeddings(model, cache_size=2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            vectors = list(pool.map(cache.embed_query, ["list the deans"] * 30))
        self.assertEqual(model.calls, 1)
        vectors[0][0] = -1
        self.assertNotEqual(cache.embed_query("list the deans")[0], -1)
        cache.embed_query("tuition")
        cache.embed_query("requirements")
        self.assertEqual(len(cache._cache), 2)
        self.assertTrue(all(isinstance(k, bytes) for k in cache._cache))
        cache.embed_query("list the deans")
        self.assertEqual(model.calls, 4)
        self.assertEqual(cache.embed_documents(["one", "two"]), [[3], [3]])

    def test_prompt_has_one_document_insertion(self):
        self.assertEqual(system_prompt.count("{retrieved_docs}"), 1)

    def test_freshness_snapshot_avoids_repeated_reads(self):
        dates = {
            freshness.doc_key("old.pdf"): {"filename": "old.pdf", "effective_date": "2024-01-01"},
            freshness.doc_key("new.pdf"): {"filename": "new.pdf", "effective_date": "2026-01-01"},
        }
        with patch.object(freshness, "_store") as factory:
            factory.return_value.load.return_value = {"docs": dates}
            snapshot = freshness.get_doc_dates()
            ranking = freshness.source_priority("old.pdf", "new.pdf", dates=snapshot)
            block = freshness.freshness_block("old.pdf", "new.pdf", dates=snapshot)
            self.assertEqual(factory.return_value.load.call_count, 1)
            self.assertTrue(ranking[freshness.doc_key("new.pdf")]["is_newest"])
            self.assertIn("2026-01-01", block)
            self.assertEqual(freshness.source_priority("old.pdf", "new.pdf", dates={}), {})
            self.assertEqual(freshness.freshness_block("old.pdf", "new.pdf", dates={}), "")
            self.assertEqual(factory.return_value.load.call_count, 1)


if __name__ == "__main__":
    unittest.main()
