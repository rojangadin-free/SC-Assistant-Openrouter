"""Offline concurrency checks, not a live 30-user provider benchmark."""

import _bootstrap  # noqa: F401
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from rag import runtime
from rag.query_embeddings import CachedQueryEmbeddings
from rag.index_client import TimedIndex
from src.prompt import system_prompt


class RuntimeTests(unittest.TestCase):
    def test_pool_queue_is_bounded_and_cancel_returns_capacity(self):
        pool = runtime.BoundedPool(1, 1, "test")
        gate = threading.Event()
        try:
            first = pool.submit(lambda: gate.wait(2))
            second = pool.submit(lambda: 2)
            with self.assertRaises(runtime.CapacityError):
                pool.submit(lambda: 3, queue_timeout=0)
            self.assertTrue(second.cancel())
            with self.assertRaises(runtime.CapacityError):
                pool.submit(lambda: 3, queue_timeout=0)
            gate.set()
            third = pool.submit(lambda: 3, queue_timeout=2)
            first.result(timeout=2)
            self.assertEqual(third.result(timeout=2), 3)
        finally:
            gate.set()
            pool.shutdown()

    def test_30_concurrent_cache_requests_compute_once_and_isolate_results(self):
        cache = runtime.TTLCache(4, 60)
        barrier = threading.Barrier(30)
        calls = []

        def compute():
            calls.append(1)
            time.sleep(0.03)
            return [{"source": "handbook", "score": 1}]

        def ask(_):
            barrier.wait(timeout=5)
            return cache.get_or_compute("list the deans", compute)

        with ThreadPoolExecutor(max_workers=30) as pool:
            results = list(pool.map(ask, range(30)))
        self.assertEqual(len(calls), 1)
        results[0][0]["score"] = 99
        self.assertEqual(results[1][0]["score"], 1)
        self.assertEqual(cache.get_or_compute("list the deans", compute)[0]["score"], 1)

    def test_cache_expiry_eviction_and_generation_namespace(self):
        cache = runtime.TTLCache(1, 1)
        with patch.object(runtime.time, "monotonic", return_value=1):
            self.assertEqual(cache.get_or_compute(("v1", "q"), lambda: 1), 1)
            self.assertEqual(cache.get_or_compute(("v2", "q"), lambda: 2), 2)
            self.assertEqual(len(cache._values), 1)
        with patch.object(runtime.time, "monotonic", return_value=3):
            self.assertEqual(cache.get_or_compute(("v2", "q"), lambda: 3), 3)

    def test_failures_are_not_cached(self):
        cache = runtime.TTLCache(1, 60)

        def fail():
            raise RuntimeError("upstream unavailable")

        with self.assertRaises(RuntimeError):
            cache.get_or_compute("q", fail)
        self.assertEqual(cache.get_or_compute("q", lambda: "recovered"), "recovered")

    def test_model_gate_returns_slots_after_failures(self):
        with patch.dict(os.environ, {"LLM_TEST_CONCURRENCY": "1", "LLM_QUEUE_TIMEOUT": "0"}):
            with runtime.model_slot("test"):
                with self.assertRaises(runtime.CapacityError):
                    with runtime.model_slot("test"):
                        pass
            with self.assertRaises(ValueError):
                with runtime.model_slot("test"):
                    raise ValueError("test")
            with runtime.model_slot("test"):
                pass

    def test_embedding_vectors_are_reused_without_mutation_leaks(self):
        class Model:
            calls = 0

            def embed_query(self, text):
                self.calls += 1
                return [1.0, 2.0]

            def embed_documents(self, texts):
                return [[3.0] for _ in texts]

        model = Model()
        embeddings = CachedQueryEmbeddings(model)
        first = embeddings.embed_query("list the deans")
        first[0] = 99
        self.assertEqual(embeddings.embed_query("list the deans"), [1.0, 2.0])
        self.assertEqual(model.calls, 1)
        self.assertEqual(embeddings.embed_documents(["a", "b"]), [[3.0], [3.0]])

    def test_documents_are_inserted_only_once(self):
        self.assertEqual(system_prompt.count("{retrieved_docs}"), 1)

    def test_index_calls_have_socket_deadlines_and_writes_check_ownership(self):
        from unittest.mock import Mock
        client = Mock()
        guard = Mock()
        index = TimedIndex(client, before_write=guard)
        index.query(vector=[1.0], top_k=1)
        self.assertGreater(client.query.call_args.kwargs["_request_timeout"], 0)
        guard.assert_not_called()
        index.upsert([{"id": "one"}])
        guard.assert_called_once()
        guard.side_effect = RuntimeError("lease lost")
        with self.assertRaises(RuntimeError):
            index.delete(filter={"source": "file.txt"})
        client.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
