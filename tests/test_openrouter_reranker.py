"""Offline local/OpenRouter ranking, protocol, safety, and backend tests."""

import ast
import concurrent.futures
import json
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401

from rag import openrouter_reranker as api
from rag import reranker
from rag.runtime import RequestBudget


def document(number, text=None, **metadata):
    return SimpleNamespace(page_content=text if text is not None else f"passage {number}",
                           metadata={"chunk_id": str(number), "source": "public.txt", **metadata})


def response_rows(scores):
    return {"results": [{"index": i, "relevance_score": score,
                         "document": {"text": "UNTRUSTED RETURNED TEXT"}}
                        for i, score in enumerate(scores)]}


class Response:
    def __init__(self, body=None, status=200, headers=None, raw=None, before_read=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw = json.dumps(body).encode() if raw is None else raw
        self.before_read = before_read
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_bytes(self):
        if self.before_read:
            self.before_read()
        for start in range(0, len(self.raw), 7):
            yield self.raw[start:start + 7]


class Client:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.handler = None

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.handler:
            return self.handler(kwargs["json"])
        return self.responses.pop(0)


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        patches = [
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key-do-not-log"}),
            patch.object(api, "MODEL_NAME", api.DEFAULT_MODEL),
            patch.object(api, "MAX_DOCUMENTS", 40),
            patch.object(api, "MAX_QUERIES", 4),
            patch.object(api, "TIMEOUT_SECONDS", 6),
            patch.object(api, "_client", self.client),
            patch.object(api, "_slots", threading.BoundedSemaphore(1)),
            patch.object(api, "_status", threading.local()),
            patch.object(api, "_cooldown_until", 0.0),
            patch.object(reranker, "RERANKER_BACKEND", "openrouter"),
            patch.object(reranker, "RERANKER_ENABLED", True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.docs = [document(i) for i in range(4)]

    def assert_fallback(self, result, expected=None):
        expected = self.docs if expected is None else expected
        self.assertEqual([d.page_content for d in result], [d.page_content for d in expected])
        self.assertTrue(all("rerank_score" not in d.metadata for d in result))
        self.assertFalse(api.available())

    def test_exact_model_endpoint_and_original_index_mapping(self):
        self.assertTrue(isinstance(api.DEFAULT_MODEL, str) and api.DEFAULT_MODEL.strip())
        self.docs[0].metadata.update(section="ADMISSIONS", private_field="NEVER_SEND_METADATA")
        rows = response_rows([0.1, 0.9, 0.5, -0.1])
        rows["results"].reverse()
        response = Response(rows)
        self.client.responses = [response]
        ranked = reranker.rerank("question", self.docs, top_k=2)
        self.assertEqual([d.metadata["chunk_id"] for d in ranked], ["1", "2"])
        self.assertEqual(ranked[0].page_content, "passage 1")
        self.assertEqual(ranked[0].metadata["rerank_score"], 0.9)
        self.assertEqual(ranked[0].metadata["rerank_model"], api.DEFAULT_MODEL)
        self.assertTrue(reranker.available())
        self.assertEqual(reranker.unavailable_reason(), "")
        method, url, options = self.client.calls[0]
        self.assertEqual((method, url), ("POST", "https://openrouter.ai/api/v1/rerank"))
        self.assertEqual(set(options["json"]), {"model", "query", "documents", "top_n"})
        self.assertEqual(options["json"]["model"], api.DEFAULT_MODEL)
        self.assertEqual(options["json"]["top_n"], 4)
        self.assertEqual(options["json"]["documents"][0], "ADMISSIONS\npassage 0")
        self.assertNotIn("NEVER_SEND_METADATA", json.dumps(options["json"]))
        self.assertEqual(options["headers"]["Authorization"], "Bearer test-key-do-not-log")
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], api.TIMEOUT_SECONDS)
        self.assertTrue(response.closed)
        self.assertTrue(all("rerank_score" not in d.metadata for d in self.docs))

    def test_query_variants_use_best_score_and_stable_ties(self):
        self.client.responses = [Response(response_rows([1, 4, 0, 0])), Response(response_rows([4, 1, 0, 0]))]
        ranked = reranker.rerank("natural", self.docs, queries=["keyword", "natural", " "])
        self.assertEqual([d.metadata["chunk_id"] for d in ranked], ["0", "1", "2", "3"])
        self.assertEqual(len(self.client.calls), 2)

    def test_preferred_query_wins_over_higher_literal_only_score(self):
        self.docs[0].page_content, self.docs[1].page_content = "LITERAL_ONLY", "CONTEXTUAL_MATCH"
        rows = {"contextual": [2, 8, 0, 0], "natural": [100, 1, 0, 0]}
        self.client.handler = lambda payload: Response(response_rows(rows[payload["query"]]))
        for preferred in (None, " ", "contextual"):
            with self.subTest(preferred=preferred):
                ranked = reranker.rerank(
                    "contextual", self.docs, top_k=2, queries=["natural"], preferred_query=preferred,
                )
                expected = ["1", "0"] if preferred == "contextual" else ["0", "1"]
                self.assertEqual([d.metadata["chunk_id"] for d in ranked], expected)
                self.assertEqual([d.metadata["rerank_score"] for d in ranked],
                                 [8, 2] if preferred == "contextual" else [100, 8])
                if preferred == "contextual":
                    self.assertTrue(all(d.metadata["rerank_aspect"] == "contextual" for d in ranked))
                else:
                    self.assertTrue(all("rerank_aspect" not in d.metadata for d in ranked))
        self.assertTrue(all("rerank_score" not in d.metadata for d in self.docs))

    def test_preferred_ties_use_max_score_then_retrieval_order(self):
        rows = {"contextual": [5, 5, 5, 4], "natural": [8, 11, 8, 100]}
        self.client.handler = lambda payload: Response(response_rows(rows[payload["query"]]))
        ranked = reranker.rerank(
            "natural", self.docs, queries=["contextual", "natural"], preferred_query="contextual",
        )
        self.assertEqual([d.metadata["chunk_id"] for d in ranked], ["1", "0", "2", "3"])
        self.assertEqual([d.metadata["rerank_score"] for d in ranked], [5, 5, 5, 4])
        self.assertEqual([call[2]["json"]["query"] for call in self.client.calls], ["contextual", "natural"])

    def test_docx_catalog_headings_reach_both_rerankers_without_duplicates(self):
        doc = document(0, "Bachelor of River Studies", section="College of River Studies",
                       section_heading="College of River Studies", collection_heading="Programs Offered",
                       private_field="NEVER_SEND_METADATA")
        before = dict(doc.metadata)
        header = "College of River Studies Programs Offered"
        self.assertEqual(reranker._header_of(doc), header)
        self.client.responses = [Response(response_rows([1]))]
        ranked = reranker.rerank("college department", [doc])
        self.assertEqual(ranked[0].page_content, doc.page_content)
        sent = self.client.calls[0][2]["json"]["documents"]
        self.assertEqual(sent, [header + "\nBachelor of River Studies"])
        self.assertEqual(doc.metadata, before)
        self.assertNotIn("NEVER_SEND_METADATA", json.dumps(sent))

    def test_each_supported_heading_field_is_available_without_source_branding(self):
        for field in ("section", "header", "section_heading", "collection_heading"):
            with self.subTest(field=field):
                doc = document(0, "Original source text", source="College_Brand.docx", **{field: "Catalog"})
                self.assertEqual(reranker._header_of(doc), "Catalog")
                self.client.responses = [Response(response_rows([1]))]
                reranker.rerank("college department", [doc])
                self.assertEqual(self.client.calls[-1][2]["json"]["documents"], ["Catalog\nOriginal source text"])

    def test_multi_part_merge_skips_shared_hits_before_choosing_next(self):
        self.client.responses = [Response(response_rows(row)) for row in (
            [10, 9, 0, 0], [10, 0, 9, 0], [10, 0, 0, 9],
        )]
        ranked = reranker.rerank_multi(["first", "second", "third"], self.docs, top_k=3)
        self.assertEqual([d.metadata["chunk_id"] for d in ranked], ["0", "2", "3"])
        self.assertEqual([d.metadata["rerank_aspect"] for d in ranked], ["first", "second", "third"])

    def test_preferred_multi_group_leads_without_replacing_natural_aspects(self):
        rows = {"contextual": [7, 8, 2, 7], "first": [9, 100, 0, 0],
                "second": [0, 100, 6, 0], "third": [0, 100, 0, 5]}
        self.client.handler = lambda payload: Response(response_rows(rows[payload["query"]]))
        aspects = ["first", "second", "contextual", "third"]
        for preferred in (None, "contextual"):
            with self.subTest(preferred=preferred):
                self.client.calls.clear()
                ranked = reranker.rerank_multi(aspects, self.docs, top_k=4, preferred_query=preferred)
                self.assertEqual([d.metadata["chunk_id"] for d in ranked],
                                 ["1", "0", "2", "3"] if preferred else ["1", "2", "0", "3"])
                expected_aspects = ["contextual", "first", "second", "third"] if preferred else aspects
                self.assertEqual([d.metadata["rerank_aspect"] for d in ranked], expected_aspects)
                self.assertEqual([call[2]["json"]["query"] for call in self.client.calls], expected_aspects)
                self.assertEqual([d.metadata["rerank_score"] for d in ranked],
                                 [8, 9, 6, 5] if preferred else [100, 6, 9, 7])

    def test_multi_part_deduplicates_repeated_chunks(self):
        docs = [document(0), document(0), document(2), document(3)]
        self.client.responses = [Response(response_rows([10, 9, 8, 1])), Response(response_rows([9, 10, 1, 8]))]
        ranked = reranker.rerank_multi(["first", "second"], docs)
        self.assertEqual(len(ranked), 3)
        self.assertEqual(len({d.metadata["chunk_id"] for d in ranked}), 3)

    def test_partial_aspect_failure_returns_original_order_without_retry(self):
        self.client.responses = [Response(response_rows([0, 9, 5, 0])), Response(status=500)]
        with patch.object(reranker, "_load_model") as local:
            result = reranker.rerank_multi(["first", "second", "third"], self.docs)
        self.assert_fallback(result)
        self.assertEqual(len(self.client.calls), 2)
        local.assert_not_called()

    def test_master_off_and_unknown_backend_never_load_or_request(self):
        with patch.object(reranker, "RERANKER_ENABLED", False), patch.object(reranker, "_load_model") as local:
            self.assert_fallback(reranker.rerank("q", self.docs))
            self.assert_fallback(reranker.rerank_multi(["a", "b"], self.docs))
            self.assertFalse(reranker.warmup())
            local.assert_not_called()
        self.assertEqual(self.client.calls, [])
        with patch.object(reranker, "RERANKER_BACKEND", "typo"), patch.object(reranker, "_model", object()):
            self.assertIsNone(reranker._load_model())
            self.assertFalse(reranker.available())
            self.assertIn("RERANKER_BACKEND", reranker.unavailable_reason())

    def test_api_warmup_is_configuration_only_and_import_is_lightweight(self):
        with patch.object(reranker, "_load_model") as local:
            self.assertTrue(reranker.warmup())
            local.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.assertFalse(reranker.available())
        self.assertNotIn("sentence_transformers", sys.modules)

    def test_startup_reports_api_configuration_without_inference(self):
        tree = ast.parse(Path("sc_assistant/__init__.py").read_text(encoding="utf-8"))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == "_warm_reranker")
        printer = Mock()
        namespace = {"print": printer}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "sc_assistant/__init__.py", "exec"), namespace)  # noqa: S102
        with patch.object(reranker, "_load_model") as local:
            namespace["_warm_reranker"]()
            local.assert_not_called()
        text = " ".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("OpenRouter reranker configured", text)
        self.assertIn(api.DEFAULT_MODEL, text)
        self.assertIn("not probed at startup", text)
        self.assertEqual(self.client.calls, [])

    def test_missing_key_empty_input_and_no_candidates_do_not_request(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            self.assert_fallback(reranker.rerank("q", self.docs, top_k=2), self.docs[:2])
            self.assertIn("OPENROUTER_API_KEY", api.unavailable_reason())
        self.assert_fallback(reranker.rerank("", self.docs))
        self.assertEqual(reranker.rerank("q", []), [])
        self.assert_fallback(reranker.rerank("q", self.docs, max_pairs=0))
        self.assertEqual(self.client.calls, [])

    def test_failure_strips_stale_scores_without_mutating_input(self):
        self.docs[0].metadata.update(rerank_score=999, rerank_aspect="old", rerank_model="old")
        self.client.responses = [Response(status=500)]
        self.assert_fallback(reranker.rerank("q", self.docs))
        self.assertEqual(self.docs[0].metadata["rerank_score"], 999)

    def test_caps_utf8_queries_documents_and_preserves_unscored_tail(self):
        docs = [document(0, ""), document(1, chr(0x00E9) * 10000), document(2), document(3)]
        self.client.responses = [Response(response_rows([0.4]))]
        with patch.object(api, "MAX_DOCUMENTS", 2):
            ranked = reranker.rerank("q" * 4000, docs)
        self.assertEqual([d.metadata["chunk_id"] for d in ranked], ["1", "0", "2", "3"])
        payload = self.client.calls[0][2]["json"]
        self.assertEqual(len(payload["documents"]), 1)
        self.assertLessEqual(len(payload["documents"][0].encode("utf-8")), 8192)
        self.assertLessEqual(len(payload["query"].encode("utf-8")), 2048)
        self.assertEqual(len(docs[1].page_content), 10000)
        self.assertNotIn("rerank_score", ranked[-1].metadata)

    def test_query_count_is_bounded(self):
        self.client.responses = [Response(response_rows([1, 2, 3, 4])) for _ in range(4)]
        reranker.rerank_multi([f"q-{i}" for i in range(10)], self.docs)
        self.assertEqual(len(self.client.calls), 4)

    def test_preferred_query_is_retained_before_cap_and_other_groups_keep_order(self):
        self.client.handler = lambda payload: Response(response_rows(
            [9, 0, 0, 0] if payload["query"] == "contextual" else [0, 9, 8, 7],
        ))
        for diversify in (False, True):
            for included in (False, True):
                with self.subTest(diversify=diversify, included=included):
                    self.client.calls.clear()
                    queries = ["first", "second", "third", "fourth", "catalog"]
                    if included:
                        queries.append("contextual")
                    ranked = api.rerank_queries(
                        queries, self.docs, diversify=diversify, preferred_query=" contextual ",
                    )
                    expected = ["contextual", "first", "second", "third"]
                    self.assertEqual([call[2]["json"]["query"] for call in self.client.calls], expected)
                    self.assertEqual(ranked[0].metadata["chunk_id"], "0")
                    self.assertEqual(ranked[0].metadata["rerank_aspect"], "contextual")
                    if diversify:
                        self.assertEqual([d.metadata["rerank_aspect"] for d in ranked], expected)

    def test_preferred_query_uses_existing_utf8_normalization_and_deduplication(self):
        preferred = " " + chr(0x00E9) * 2000 + " "
        normalized = preferred.strip().encode("utf-8")[:2048].decode("utf-8")
        rows = {normalized: [2, 8, 0, 0], "natural": [100, 1, 0, 0]}
        self.client.handler = lambda payload: Response(response_rows(rows[payload["query"]]))
        ranked = reranker.rerank(
            "natural", self.docs, queries=[preferred.strip(), preferred.strip() + "suffix"],
            preferred_query=preferred,
        )
        self.assertEqual([call[2]["json"]["query"] for call in self.client.calls], [normalized, "natural"])
        self.assertEqual(ranked[0].metadata["chunk_id"], "1")
        self.assertEqual(ranked[0].metadata["rerank_score"], 8)
        self.assertEqual(ranked[0].metadata["rerank_aspect"], normalized)

    def test_preferred_calls_preserve_configuration_off_and_failure_fallbacks(self):
        def calls():
            return (
                reranker.rerank("natural", self.docs, top_k=2, preferred_query="contextual"),
                reranker.rerank_multi(["first", "second"], self.docs, top_k=2, preferred_query="contextual"),
            )

        with patch.object(reranker, "_load_model") as local:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
                for result in calls():
                    self.assert_fallback(result, self.docs[:2])
            with patch.object(api, "MODEL_NAME", ""):
                for result in calls():
                    self.assert_fallback(result, self.docs[:2])
            with patch.object(reranker, "RERANKER_ENABLED", False):
                for result in calls():
                    self.assert_fallback(result, self.docs[:2])
            self.assertEqual(self.client.calls, [])
            self.client.handler = Mock(side_effect=RuntimeError("fixture model unavailable"))
            for result in calls():
                self.assert_fallback(result, self.docs[:2])
            self.assertEqual(len(self.client.calls), 2)
            local.assert_not_called()

    def test_invalid_responses_fail_closed(self):
        bad = [None, {}, {"results": []}, {"results": [{"index": 0, "relevance_score": 1}]},
               {"results": [None] * 4}]
        for field, value in (("index", True), ("index", -1), ("index", 4), ("index", 1),
                             ("relevance_score", True), ("relevance_score", "0.8"),
                             ("relevance_score", float("nan")), ("relevance_score", float("inf"))):
            body = response_rows([1, 2, 3, 4])
            body["results"][0][field] = value
            bad.append(body)
        for body in bad:
            with self.subTest(body=body):
                response = Response(body)
                self.client.responses = [response]
                self.assert_fallback(reranker.rerank("q", self.docs))
                self.assertTrue(response.closed)
        self.client.responses = [Response(raw=b"not-json")]
        self.assert_fallback(reranker.rerank("q", self.docs))

    def test_size_limit_closes_response_and_returns_capacity(self):
        response = Response(raw=b"x" * 40)
        self.client.responses = [response]
        with patch.object(api, "MAX_RESPONSE_BYTES", 10):
            self.assert_fallback(reranker.rerank("q", self.docs))
        self.assertTrue(response.closed)
        self.client.responses = [Response(response_rows([1, 2, 3, 4]))]
        self.assertEqual(reranker.rerank("q", self.docs)[0].metadata["chunk_id"], "3")

    def test_rate_limit_honors_cooldown_without_sleep_retry_or_paid_fallback(self):
        self.client.responses = [Response(status=429, headers={"Retry-After": "60"})]
        self.assert_fallback(reranker.rerank("q", self.docs))
        self.assert_fallback(reranker.rerank("again", self.docs))
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn("cooldown", api.unavailable_reason())
        self.assertEqual(self.client.calls[0][2]["json"]["model"], api.DEFAULT_MODEL)

    def test_retry_after_http_date_is_supported(self):
        header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
        self.client.responses = [Response(status=503, headers={"Retry-After": header})]
        self.assert_fallback(reranker.rerank("q", self.docs))
        self.assert_fallback(reranker.rerank("q", self.docs))
        self.assertEqual(len(self.client.calls), 1)
        self.assertGreater(api._cooldown_until - api.time.monotonic(), 50)

    def test_http_errors_and_redirects_are_not_successful_rankings(self):
        for status in (301, 400, 401, 402, 403, 404, 500, 502, 503):
            with self.subTest(status=status), patch.object(api, "_cooldown_until", 0.0):
                before = len(self.client.calls)
                self.client.responses = [Response(status=status)]
                self.assert_fallback(reranker.rerank("q", self.docs))
                self.assertEqual(len(self.client.calls), before + 1)

    def test_expired_or_cancelled_request_does_not_start_http(self):
        for budget in (RequestBudget(-1), RequestBudget(5)):
            budget.cancelled.set()
            self.assert_fallback(reranker.rerank("q", self.docs, request_budget=budget))
        self.assertEqual(self.client.calls, [])

    def test_stage_deadline_stops_before_another_aspect(self):
        clock = {"now": 0.0}
        response = Response(response_rows([1, 2, 3, 4]), before_read=lambda: clock.update(now=20.0))
        self.client.responses = [response]
        with patch.object(api.time, "monotonic", side_effect=lambda: clock["now"]):
            self.assert_fallback(reranker.rerank_multi(["a", "b"], self.docs))
        self.assertEqual(len(self.client.calls), 1)
        self.assertTrue(response.closed)

    def test_concurrency_is_bounded_and_status_is_request_thread_local(self):
        entered, release = threading.Event(), threading.Event()

        def hold():
            entered.set()
            release.wait(3)

        self.client.handler = lambda _: Response(response_rows([1, 2, 3, 4]), before_read=hold)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(reranker.rerank, "first", self.docs)
            try:
                self.assertTrue(entered.wait(2))
                self.assert_fallback(reranker.rerank("second", self.docs))
                self.assertEqual(len(self.client.calls), 1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3)[0].metadata["chunk_id"], "3")
        self.assertFalse(api.available())
        self.client.handler = lambda _: Response(response_rows([1, 2, 3, 4]))
        reranker.rerank("third", self.docs)
        self.assertTrue(api.available())

    def test_transport_exceptions_do_not_expose_credentials_or_data(self):
        self.client.handler = Mock(side_effect=RuntimeError("test-key-do-not-log PRIVATE_QUERY PRIVATE_DOC"))
        with self.assertLogs(api.logger, level="INFO") as logs:
            self.assert_fallback(reranker.rerank("PRIVATE_QUERY", self.docs))
        output = " ".join(logs.output)
        self.assertNotIn("test-key-do-not-log", output)
        self.assertNotIn("PRIVATE_QUERY", output)
        self.assertNotIn("PRIVATE_DOC", output)

    def test_client_is_lazy_bounded_and_refuses_redirects(self):
        factory = Mock(return_value=self.client)
        limits = Mock()
        with patch.object(api, "_client", None), patch.dict(sys.modules, {
            "httpx": SimpleNamespace(Client=factory, Limits=limits),
        }):
            self.assertIs(api._http_client(), self.client)
            self.assertIs(api._http_client(), self.client)
        factory.assert_called_once()
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])
        limits.assert_called_once_with(max_connections=api.CONCURRENCY,
                                       max_keepalive_connections=api.CONCURRENCY)


class LocalRerankerTests(unittest.TestCase):
    def setUp(self):
        self.model = Mock()
        self.loader = Mock(return_value=self.model)
        for p in (
            patch.object(reranker, "RERANKER_BACKEND", "local"),
            patch.object(reranker, "RERANKER_ENABLED", True),
            patch.object(reranker, "_load_model", self.loader),
            patch.object(reranker, "_slots", threading.BoundedSemaphore(1)),
            patch.object(reranker, "MAX_TOTAL_WINDOWS", 220),
            patch.object(reranker, "WINDOW_CHARS", 900),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.docs = [document(i) for i in range(4)]

    def use_scores(self, rows):
        indices = {doc.page_content: i for i, doc in enumerate(self.docs)}
        self.model.predict.side_effect = lambda pairs, **_: [rows[q][indices[text]] for q, text in pairs]

    def test_preferred_best_window_wins_over_higher_literal_only_score(self):
        self.docs = [document(0, "LITERAL_ONLY"), document(1, "CONTEXTUAL_MATCH")]
        rows = {("contextual", "LITERAL_ONLY:first"): 2, ("contextual", "LITERAL_ONLY:answer"): 1,
                ("contextual", "CONTEXTUAL_MATCH:first"): -20, ("contextual", "CONTEXTUAL_MATCH:answer"): 8,
                ("natural", "LITERAL_ONLY:first"): 100, ("natural", "LITERAL_ONLY:answer"): 99,
                ("natural", "CONTEXTUAL_MATCH:first"): 5, ("natural", "CONTEXTUAL_MATCH:answer"): 4}
        self.model.predict.side_effect = lambda pairs, **_: [rows[pair] for pair in pairs]
        with patch.object(reranker, "_windows_of", side_effect=lambda doc: [
            doc.page_content + ":first", doc.page_content + ":answer",
        ]):
            for preferred in (None, " ", "contextual"):
                with self.subTest(preferred=preferred):
                    ranked = reranker.rerank(
                        "contextual", self.docs, top_k=2, queries=["natural"], preferred_query=preferred,
                    )
                    self.assertEqual([d.metadata["chunk_id"] for d in ranked],
                                     ["1", "0"] if preferred == "contextual" else ["0", "1"])
                    self.assertEqual([d.metadata["rerank_score"] for d in ranked],
                                     [8, 2] if preferred == "contextual" else [100, 8])
                    if preferred == "contextual":
                        self.assertTrue(all(d.metadata["rerank_aspect"] == "contextual" for d in ranked))
                    self.assertEqual(len(self.model.predict.call_args.args[0]), 8)

    def test_preferred_ties_use_max_score_then_retrieval_order(self):
        self.use_scores({"contextual": [5, 5, 5, 4], "natural": [8, 11, 8, 100]})
        ranked = reranker.rerank(
            "natural", self.docs, queries=["contextual", "natural"], preferred_query="contextual",
        )
        self.assertEqual([d.metadata["chunk_id"] for d in ranked], ["1", "0", "2", "3"])
        self.assertEqual([d.metadata["rerank_score"] for d in ranked], [5, 5, 5, 4])
        self.assertEqual(list(dict.fromkeys(q for q, _ in self.model.predict.call_args.args[0])),
                         ["contextual", "natural"])

    def test_preferred_multi_group_leads_without_replacing_natural_aspects(self):
        self.use_scores({"contextual": [7, 8, 2, 7], "first": [9, 100, 0, 0],
                         "second": [0, 100, 6, 0], "third": [0, 100, 0, 5]})
        aspects = ["first", "second", "contextual", "third"]
        for preferred in (None, "contextual"):
            with self.subTest(preferred=preferred):
                self.model.predict.reset_mock()
                ranked = reranker.rerank_multi(aspects, self.docs, top_k=4, preferred_query=preferred)
                self.assertEqual([d.metadata["chunk_id"] for d in ranked],
                                 ["1", "0", "2", "3"] if preferred else ["1", "2", "0", "3"])
                expected_aspects = ["contextual", "first", "second", "third"] if preferred else aspects
                self.assertEqual([d.metadata["rerank_aspect"] for d in ranked], expected_aspects)
                self.assertEqual([d.metadata["rerank_score"] for d in ranked],
                                 [8, 9, 6, 5] if preferred else [100, 6, 9, 7])
                self.assertEqual(list(dict.fromkeys(q for q, _ in self.model.predict.call_args.args[0])),
                                 expected_aspects)
                self.model.predict.assert_called_once()

    def test_missing_preferred_query_is_added_without_losing_multi_aspects(self):
        self.docs.append(document(4))
        aspects = ["first", "second", "third", "fourth"]
        rows = {aspect: [9 if i == a_i else 0 for i in range(5)] for a_i, aspect in enumerate(aspects)}
        rows["contextual"] = [0, 0, 0, 0, 8]
        self.use_scores(rows)
        ranked = reranker.rerank("first", self.docs, queries=["second"], preferred_query="contextual")
        self.assertEqual(ranked[0].metadata["chunk_id"], "4")
        self.model.predict.reset_mock()
        ranked = reranker.rerank_multi(aspects, self.docs, top_k=5, preferred_query="contextual")
        self.assertEqual([d.metadata["chunk_id"] for d in ranked], ["4", "0", "1", "2", "3"])
        self.assertEqual([d.metadata["rerank_aspect"] for d in ranked], ["contextual", *aspects])
        self.assertEqual(list(dict.fromkeys(q for q, _ in self.model.predict.call_args.args[0])),
                         ["contextual", *aspects])
        self.model.predict.assert_called_once()

    def test_single_preferred_aspect_propagates_to_ordinary_rerank(self):
        self.use_scores({"contextual": [1, 8, 0, 0]})
        ranked = reranker.rerank_multi(
            ["contextual", " contextual ", " "], self.docs, preferred_query=" contextual ",
        )
        self.assertEqual(ranked[0].metadata["chunk_id"], "1")
        self.assertEqual(ranked[0].metadata["rerank_score"], 8)
        self.assertEqual(ranked[0].metadata["rerank_aspect"], "contextual")
        self.assertEqual(len(self.model.predict.call_args.args[0]), len(self.docs))

    def test_preferred_calls_keep_retrieval_order_when_model_is_unavailable(self):
        self.loader.return_value = None
        for result in (
            reranker.rerank("natural", self.docs, top_k=2, preferred_query="contextual"),
            reranker.rerank_multi(["first", "second"], self.docs, top_k=2, preferred_query="contextual"),
        ):
            self.assertEqual([d.metadata["chunk_id"] for d in result], ["0", "1"])
            self.assertTrue(all("rerank_score" not in d.metadata for d in result))
        self.model.predict.assert_not_called()

    def test_preferred_calls_keep_failure_and_busy_fallbacks(self):
        budget = object()
        for failure in (RuntimeError("fixture scoring failure"), None):
            with self.subTest(failure=failure), patch.object(
                reranker, "_predict", side_effect=failure, return_value=None,
            ), patch.object(reranker, "rerank", wraps=reranker.rerank) as ordinary:
                ranked = ordinary("natural", self.docs, top_k=2, preferred_query="contextual")
                self.assertEqual([d.metadata["chunk_id"] for d in ranked], ["0", "1"])
                ordinary.reset_mock()
                ranked = reranker.rerank_multi(
                    ["first", "second"], self.docs, top_k=2,
                    request_budget=budget, preferred_query="contextual",
                )
                self.assertEqual([d.metadata["chunk_id"] for d in ranked], ["0", "1"])
                self.assertTrue(all("rerank_score" not in d.metadata for d in ranked))
                if failure is not None:
                    ordinary.assert_called_once_with(
                        "contextual", self.docs, top_k=2, max_pairs=reranker.MAX_PAIRS,
                        request_budget=budget, preferred_query="contextual",
                    )
                else:
                    ordinary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
