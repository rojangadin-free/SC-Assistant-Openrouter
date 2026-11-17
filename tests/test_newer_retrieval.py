"""Offline regressions for newer-first acquisition, selection, and chat context.

Run: python -B -X utf8 tests/test_newer_retrieval.py -v
Only selected real chain definitions execute; no application startup or corpus.
"""

import __future__

import ast
import builtins
import concurrent.futures
import datetime
import importlib.util
import io
import os
import re
import sys
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BASE = "Campus-Handbook-2024.pdf"
PROGRAMS = "Programs_Offered (Current).DOCX"
STAMP = "2026-11-13T02:39:28Z"
QUESTION = "List the academic departments"


def definitions(relative_path, names, namespace):
    """Extract explicit definitions, never module-level clients or imports."""
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    selected, found = [], set()
    for node in tree.body:
        declared = set()
        if isinstance(node, ast.FunctionDef):
            declared.add(node.name)
        elif isinstance(node, ast.Assign):
            declared.update(part.id for target in node.targets for part in ast.walk(target)
                            if isinstance(part, ast.Name))
        if declared & names:
            selected.append(node)
            found.update(declared & names)
    if names - found:
        raise AssertionError(f"Missing AST interfaces in {relative_path}: {names - found}")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec",  # noqa: S102 - whitelisted local AST
                 flags=__future__.annotations.compiler_flag, dont_inherit=True), namespace)


def document(source, text, chunk_id, **metadata):
    return SimpleNamespace(page_content=text, metadata={
        "source": source, "chunk_id": chunk_id, "page": 1, **metadata,
    })


class Graph:
    def __init__(self, state_type):
        self.node = None

    def add_node(self, name, node):
        self.node = node

    def set_entry_point(self, name):
        pass

    def add_edge(self, start, end):
        pass

    def compile(self):
        return self.node


@dataclass(frozen=True)
class Leaf:
    invoke: object
    alpha: float
    top_k: int = 30
    filter: object = None


class Hybrid:
    """Strict per-call leaf filters and a small weighted-RRF double."""

    def __init__(self, current, baseline):
        self.current, self.baseline = current, baseline
        self.calls = []
        self.filtered = self.matching
        self.sparse = Leaf(Mock(side_effect=lambda q, **kw: self.fetch("sparse", q, **kw)), 0.0)
        self.dense = Leaf(Mock(side_effect=lambda q, **kw: self.fetch("dense", q, **kw)), 1.0)
        self.ensemble = SimpleNamespace(
            invoke=Mock(side_effect=self.unfiltered),
            weighted_reciprocal_rank=Mock(side_effect=self.rrf),
            weights=(0.45, 0.55), retrievers=(self.sparse, self.dense),
        )

    def matching(self, query, metadata_filter):
        source = metadata_filter["source"]
        names = [source["$eq"]] if "$eq" in source else source["$in"]
        return [doc for doc in self.current if doc.metadata["source"] in names]

    def fetch(self, leaf, query, *, filter):
        self.calls.append((leaf, query, deepcopy(filter)))
        return self.filtered(query, filter)

    def unfiltered(self, query):
        self.calls.append(("ensemble", query, None))
        return list(self.baseline)

    def rrf(self, lists):
        documents, scores = {}, {}
        for weight, docs in zip(self.ensemble.weights, lists):
            for rank, doc in enumerate(docs, 1):
                key = doc.metadata["chunk_id"]
                documents.setdefault(key, doc)
                scores[key] = scores.get(key, 0) + weight / (60 + rank)
        return [documents[key] for key in sorted(documents, key=scores.get, reverse=True)]


class NewerRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="newer-retrieval-"))
        vocab = Path(temporary) / "vocabulary.json"
        vocab.write_text('{"terms": {}}', encoding="utf-8")
        self.stack.enter_context(patch.dict(os.environ, {
            "SC_SPELL_VOCAB": str(vocab), "RERANKER_BACKEND": "local",
            "RERANKER_ENABLED": "false", "RAG_DEBUG": "false",
            "RETRIEVAL_TIMEOUT_SECONDS": "30",
        }))

        self.denied_imports, self.env_reads = [], []
        original_import = builtins.__import__
        forbidden = {
            "app", "config", "dotenv", "aws", "src", "store_index", "sc_assistant",
            "boto3", "botocore", "pinecone", "pinecone_text", "langchain",
            "langchain_core", "langchain_classic", "langchain_community", "langgraph",
            "langchain_openai", "langchain_huggingface", "sentence_transformers",
            "torch", "transformers", "openai", "rag.chain", "rag.store",
            "rag.conflicts", "rag.openrouter_reranker",
        }

        def offline_import(name, *args, **kwargs):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden):
                self.denied_imports.append(name)
                raise AssertionError(f"Live dependency forbidden: {name}")
            return original_import(name, *args, **kwargs)

        self.stack.enter_context(patch.dict(sys.modules, dict.fromkeys(forbidden)))
        self.stack.enter_context(patch("builtins.__import__", new=offline_import))

        def offline_open(opener):
            def guarded(path, *args, **kwargs):
                if isinstance(path, (str, os.PathLike)) and Path(path).name.startswith(".env"):
                    self.env_reads.append(os.fspath(path))
                    raise AssertionError("No .env reads")
                return opener(path, *args, **kwargs)
            return guarded

        self.stack.enter_context(patch("builtins.open", new=offline_open(builtins.open)))
        self.stack.enter_context(patch("io.open", new=offline_open(io.open)))
        self.sockets = [self.stack.enter_context(patch(target, side_effect=AssertionError("No network")))
                        for target in ("socket.create_connection", "socket.socket.connect",
                                       "socket.socket.connect_ex", "socket.getaddrinfo")]
        self.addCleanup(self.assert_offline)
        package = ModuleType("rag")
        package.__path__ = [str(ROOT / "rag")]
        self.stack.enter_context(patch.dict(sys.modules, {"rag": package}))

        self.freshness = ModuleType("rag.freshness")
        self.freshness.__dict__.update(os=os, re=re, datetime=datetime,
                                      get_doc_dates=Mock(side_effect=AssertionError("No store reads")))
        definitions("rag/freshness.py", {
            "doc_key", "_YEAR_MIN", "_YEAR_MAX", "_valid_year", "date_from_filename",
            "_parse_iso_date", "source_priority", "freshness_block",
        }, self.freshness.__dict__)
        self.stack.enter_context(patch.dict(sys.modules, {"rag.freshness": self.freshness}))

        spelling = self.module("spelling")
        optimizer = self.module("query_optimizer")
        language, dictation = self.module("language"), self.module("dictation")
        latency, self.runtime = self.module("latency"), self.module("runtime")
        self.reranker, self.policy = self.module("reranker"), self.module("source_policy")
        self.spelling, self.optimizer = spelling, optimizer

        class Today(datetime.date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 15)

        self.stack.enter_context(patch.object(self.policy, "date", Today))
        self.pool = self.runtime.BoundedExecutor(4, 24, "newer-test")
        self.addCleanup(self.pool.shutdown)
        self.maps = Mock(wraps=self.pool.map_ordered)
        self.dates = {BASE.lower(): {"filename": BASE, "effective_date": "2024-06-01"}}
        self.uploads = {}
        self.old = [document(BASE, f"Archived college departments and enrollment: OLD_RULE_{i}.",
                             f"old-{i}", page=i + 1) for i in range(45)]
        self.selections = Mock(wraps=self.policy.select_documents)
        self.ranking = Mock(wraps=self.reranker.rerank)
        self.multi_ranking = Mock(wraps=self.reranker.rerank_multi)
        self.summary = Mock(return_value="STALE_SUMMARY_FACT")

        def optimize(messages):
            question = messages[-1]["content"].split("\n\nLatest Question: ", 1)[1]
            return SimpleNamespace(content=question.split("\nLIKELY MISSPELLINGS", 1)[0])

        self.ns = {
            "os": os, "re": re, "concurrent": concurrent, "datetime": datetime.datetime,
            "StateGraph": Graph, "ChatState": dict, "END": None,
            "HumanMessage": SimpleNamespace, "SystemMessage": SimpleNamespace,
            "correct_query": spelling.correct_query, "spelling_variants": spelling.spelling_variants,
            "dictation_variants": dictation.dictation_variants, "language_variants": language.language_variants,
            "optimize_query": optimizer.optimize_query,
            "should_optimize": latency.should_optimize, "pick_search_query": latency.pick_search_query,
            "Deadline": latency.Deadline, "Timings": latency.Timings,
            "CapacityError": self.runtime.CapacityError, "RequestBudget": self.runtime.RequestBudget,
            "env_int": self.runtime.env_int, "retrieval_pool": SimpleNamespace(map_ordered=self.maps),
            "preparation_pool": self.pool, "optimizer_pool": self.pool,
            "rerank": self.ranking, "rerank_multi": self.multi_ranking,
            "select_documents": self.selections, "SOURCE_SELECTION_GUIDANCE": self.policy.SOURCE_SELECTION_GUIDANCE,
            "get_doc_dates": Mock(side_effect=lambda: self.dates),
            "get_file_upload_dates": Mock(side_effect=lambda: self.uploads),
            "doc_key": self.freshness.doc_key, "source_priority": Mock(wraps=self.freshness.source_priority),
            "freshness_block": Mock(wraps=self.freshness.freshness_block),
            "query_optimizer_model": SimpleNamespace(invoke=Mock(side_effect=optimize)),
            "summarize_history": self.summary, "authority_block": Mock(return_value=""),
            "resolve_role": lambda *args, **kwargs: "student", "role_block": lambda role: "",
            "emit_phase": Mock(), "response_language_prompt": "Use the latest question's language.",
            "system_prompt": "Documents:\n{retrieved_docs}\nHistory:\n{chat_history}\n{document_dates}",
            "build_citations": Mock(side_effect=lambda docs: [d.metadata["chunk_id"] for d in docs]),
        }
        definitions("rag/chain.py", {
            "_STOPWORDS", "THIN_QUERY_TERMS", "_QUESTION_WORDS", "_DETERMINERS",
            "MIN_PART_TERMS", "MAX_PARTS", "RERANK_CANDIDATES", "FINAL_TOP_K",
            "_content_terms", "expand_queries", "split_question", "multi_query_retrieve",
            "retrieve_documents", "docs_to_context", "safe_prompt", "create_graph",
        }, self.ns)
        self.retrieval = Mock(wraps=self.ns["retrieve_documents"])
        self.ns["retrieve_documents"] = self.retrieval
        self.install([])
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))

    def module(self, name):
        qualified = f"rag.{name}"
        spec = importlib.util.spec_from_file_location(qualified, ROOT / "rag" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        self.stack.enter_context(patch.dict(sys.modules, {qualified: module}))
        spec.loader.exec_module(module)
        return module

    def assert_offline(self):
        self.assertEqual(self.denied_imports, [])
        self.assertEqual(self.env_reads, [])
        for connection in self.sockets:
            connection.assert_not_called()
        self.freshness.get_doc_dates.assert_not_called()

    def install(self, current, baseline=None):
        self.hybrid = Hybrid(current, self.old if baseline is None else baseline)
        self.ns.update(retriever=self.hybrid.ensemble, sparse_retriever=self.hybrid.sparse,
                       dense_retriever=self.hybrid.dense)
        self.uploads = dict.fromkeys((d.metadata["source"] for d in current), STAMP)
        return self.hybrid

    def retrieve(self, question=QUESTION, **kwargs):
        kwargs.setdefault("allow_spelling", False)
        return self.ns["retrieve_documents"](question, **kwargs)

    def chat(self, question=QUESTION, history=None, **state_fields):
        state = {"input": question, "chat_history": history or [], "role": "student", **state_fields}
        before = deepcopy(state)
        result = self.ns["create_graph"]()(state)
        self.assertEqual(state, before)
        return result

    def test_query_only_output_is_not_waited_for_until_retrieval_enters(self):
        current = document("Enrollment_Update.docx", "Enrollment requires CURRENT_FORM and application documents.", "current")
        self.install([current])
        typed, natural = "enrollmnet requirements", "enrollment requirements"
        keywords = "enrollment application registration form requirements"
        history = [{"role": "user", "content": "How do I enroll?"}]
        correction = Mock(wraps=self.spelling.correct_query)
        model = self.ns["query_optimizer_model"].invoke
        model.side_effect = None
        model.return_value = SimpleNamespace(content=f"\n {keywords} \n")
        future = concurrent.futures.Future()
        future.result = Mock(wraps=future.result)

        def submit(task):
            future.set_result(task())
            return future

        def retrieve(query, **kwargs):
            self.assertEqual(query, natural)
            self.assertEqual(kwargs["user_question"], natural)
            self.assertIs(kwargs.get("allow_spelling", True), True)
            future.result.assert_not_called()
            companion = kwargs["late_query"]()
            self.assertEqual(companion, keywords)
            selection = self.policy.select_documents(
                [current], [query, companion], dates=kwargs["dates"], uploads=kwargs["uploads"],
            )
            kwargs["on_selection"](selection)
            return selection.documents

        retrieval = Mock(side_effect=retrieve)
        with patch.dict(self.ns, {
            "correct_query": correction, "retrieve_documents": retrieval,
            "optimizer_pool": SimpleNamespace(submit=Mock(side_effect=submit)),
        }):
            result = self.chat(typed, history)
        correction.assert_called_once_with(typed)
        model.assert_called_once_with(self.optimizer.optimizer_messages(natural, history))
        retrieval.assert_called_once()
        future.result.assert_called_once()
        self.assertEqual(result["messages_to_llm"][-1].content, natural)
        self.assertEqual(result["citations"], ["current"])
        self.assertEqual(result["chat_history"], history)
        self.ns["authority_block"].assert_called_once()
        self.assertEqual(self.ns["authority_block"].call_args.args[:2], (natural, natural))

    def test_late_companion_retrieves_keyword_only_hit_without_becoming_primary(self):
        question, companion = "enrollment requirements", "application registration form checklist"
        current = document("Enrollment_Update.docx", "Enrollment requirements use CURRENT_FORM and its application checklist.", "current")
        hybrid = self.install([current])
        hybrid.filtered = lambda query, filters: [current] if query == companion else []
        self.ns["query_optimizer_model"].invoke.side_effect = None
        self.ns["query_optimizer_model"].invoke.return_value = SimpleNamespace(content=companion)
        result = self.chat(question)
        self.assertEqual(result["citations"], ["current"])
        self.assertEqual(result["messages_to_llm"][-1].content, question)
        self.assertEqual(self.retrieval.call_args.args[0], question)
        self.assertEqual(self.retrieval.call_args.kwargs["user_question"], question)
        self.assertTrue(callable(self.retrieval.call_args.kwargs["late_query"]))
        self.assertIs(self.retrieval.call_args.kwargs.get("allow_spelling", True), True)
        self.assertEqual(self.ranking.call_args.args[0], question)
        self.assertEqual(self.ranking.call_args.kwargs["queries"], [companion])
        self.assertEqual({query for leaf, query, filters in hybrid.calls}, {question, companion})
        for call in self.selections.call_args_list:
            self.assertEqual(call.args[1][0], question)
        self.assertIn("CURRENT_FORM", result["messages_to_llm"][0].content)
        hybrid.ensemble.invoke.assert_not_called()

    def test_optimizer_error_blank_and_echo_fall_back_without_forced_clarification(self):
        question = "enrollment requirements"
        current = document("Enrollment_Update.docx", "Enrollment requires CURRENT_FORM only.", "current")
        history = [{"role": "assistant", "content": "STALE_ENROLLMENT_FACT"}]
        model = self.ns["query_optimizer_model"].invoke
        for output in (RuntimeError("optimizer unavailable"), "", "   ", question, question.upper()):
            with self.subTest(output=repr(output)):
                self.install([current])
                model.reset_mock()
                model.side_effect = output if isinstance(output, Exception) else None
                model.return_value = SimpleNamespace(content=output)
                result = self.chat(question, history)
                model.assert_called_once_with(self.optimizer.optimizer_messages(question, history))
                self.assertEqual(result["citations"], ["current"])
                self.assertEqual(result["messages_to_llm"][-1].content, question)
                self.assertEqual(self.retrieval.call_args.args[0], question)
                self.assertEqual(self.ranking.call_args.args[0], question)
                self.assertEqual(self.ranking.call_args.kwargs["queries"], [])
                self.assertEqual({query for leaf, query, filters in self.hybrid.calls}, {question})
                prompt = result["messages_to_llm"][0].content
                for marker in ("<query_interpretation>", "<accepted_offer>", "Ask ONLY one", "STALE_ENROLLMENT_FACT"):
                    self.assertNotIn(marker, prompt)

    def test_pending_or_failed_optimizer_future_uses_natural_retrieval(self):
        question = "enrollment requirements"
        current = document("Enrollment_Update.docx", "Enrollment requires CURRENT_FORM only.", "current")
        for mode in ("pending", "failed"):
            with self.subTest(mode=mode):
                self.install([current])
                future = concurrent.futures.Future()
                if mode == "pending":
                    future.set_running_or_notify_cancel()
                else:
                    future.set_exception(RuntimeError("worker failed"))
                with patch.dict(self.ns, {
                    "optimizer_pool": SimpleNamespace(submit=Mock(return_value=future)),
                    "Deadline": lambda: SimpleNamespace(remaining=lambda: 0),
                }):
                    result = self.chat(question)
                if mode == "pending":
                    future.set_result("LATE_QUERY_MUST_NOT_JOIN")
                self.assertEqual(result["citations"], ["current"])
                self.assertEqual(result["messages_to_llm"][-1].content, question)
                self.assertEqual(self.ranking.call_args.args[0], question)
                self.assertEqual(self.ranking.call_args.kwargs["queries"], [])
                self.assertNotIn("LATE_QUERY_MUST_NOT_JOIN", {query for leaf, query, filters in self.hybrid.calls})
                self.assertNotIn("<query_interpretation>", result["messages_to_llm"][0].content)
        self.ns["query_optimizer_model"].invoke.assert_not_called()

    def test_optimizer_capacity_refusal_preserves_context_without_understanding_phase(self):
        question = "enrollment requirements"
        current = document("Enrollment_Update.docx", "Enrollment requires CURRENT_FORM only.", "current")
        self.install([current])
        with patch.dict(self.ns, {
            "optimizer_pool": SimpleNamespace(submit=Mock(side_effect=self.runtime.CapacityError("busy"))),
        }):
            result = self.chat(question)
        self.assertEqual(result["citations"], ["current"])
        self.assertEqual(result["messages_to_llm"][-1].content, question)
        self.assertEqual(self.ranking.call_args.kwargs["queries"], [])
        self.assertNotIn("understanding", [call.args[1] for call in self.ns["emit_phase"].call_args_list])
        self.assertNotIn("<query_interpretation>", result["messages_to_llm"][0].content)
        self.ns["query_optimizer_model"].invoke.assert_not_called()

    def test_complete_clean_question_skips_optimizer_without_dropping_retrieval(self):
        question = "graduate scholarship application eligibility requirements"
        current = document("Scholarship_Update.docx", "Graduate scholarship application eligibility requirements use CURRENT_FORM.", "current")
        self.install([current])
        result = self.chat(question, [{"role": "user", "content": "enrollment"}])
        self.ns["query_optimizer_model"].invoke.assert_not_called()
        self.assertEqual(result["citations"], ["current"])
        self.assertEqual(self.retrieval.call_args.args[0], question)
        self.assertEqual(result["messages_to_llm"][-1].content, question)
        self.assertEqual(self.ranking.call_args.kwargs["queries"], [])

    def test_context_followup_rewrite_uses_history_only_for_a_late_probe(self):
        question, companion = "How much?", "transcript request fee schedule"
        current = document("Transcript_Fees.docx", "Transcript request fee schedule: CURRENT_FEE_RECORD.", "current")
        self.install([current])
        history = [{"role": "user", "content": "What student documents are available?"},
                   {"role": "assistant", "content": "The registrar handles transcript requests. STALE_PRICE_500."},
                   {"role": "user", "content": "How long?"},
                   {"role": "assistant", "content": "STALE_PROCESSING_TIME"}]
        model = self.ns["query_optimizer_model"].invoke
        model.side_effect = None
        model.return_value = SimpleNamespace(content=companion)
        result = self.chat(question, history)
        model.assert_called_once_with(self.optimizer.optimizer_messages(question, history))
        self.assertEqual(self.retrieval.call_args.args[0], question)
        self.assertEqual(self.retrieval.call_args.kwargs["user_question"], question)
        self.assertEqual(self.ranking.call_args.args[0], question)
        self.assertIn(companion, self.ranking.call_args.kwargs["queries"])
        self.assertEqual(result["messages_to_llm"][-1].content, question)
        self.assertEqual(result["citations"], ["current"])
        self.assertEqual(result["chat_history"], history)
        prompt = result["messages_to_llm"][0].content
        self.assertIn("What student documents are available?", prompt)
        for marker in ("STALE_PRICE_500", "STALE_PROCESSING_TIME", "<query_interpretation>", "<accepted_offer>"):
            self.assertNotIn(marker, prompt)

    def test_image_analysis_and_query_rewrite_keep_separate_handoffs(self):
        current = document("Enrollment_Update.docx", "Enrollment requires CURRENT_FORM.", "current")
        self.install([current])
        question, natural, image = "enrollmnet", "enrollment", "offline-image-data"
        companion, description = "enrollment application form", "OCR_ENROLLMENT_FORM"
        model = self.ns["query_optimizer_model"].invoke
        model.side_effect = None
        model.return_value = SimpleNamespace(content=companion)
        vision = Mock(return_value=SimpleNamespace(content=description))
        with patch.dict(self.ns, {"summarizer": SimpleNamespace(invoke=vision)}):
            result = self.chat(question, image_data=image)
        model.assert_called_once_with(self.optimizer.optimizer_messages(natural))
        vision.assert_called_once()
        image_url = f"data:image/jpeg;base64,{image}"
        self.assertEqual(vision.call_args.args[0][0].content[1]["image_url"]["url"], image_url)
        content = result["messages_to_llm"][-1].content
        self.assertEqual(content[0]["text"], f"{natural}\n\n[System Note - Image Analysis Provided]: {description}")
        self.assertEqual(content[1]["image_url"]["url"], image_url)
        self.assertEqual(self.retrieval.call_args.args[0], natural)
        self.assertEqual(self.retrieval.call_args.kwargs["user_question"], natural)
        self.assertIn(companion, self.ranking.call_args.kwargs["queries"])
        self.assertEqual(result["citations"], ["current"])

    def test_numbered_followup_uses_model_history_and_keeps_the_literal_reply(self):
        current = document("Institution_History.docx", "History of Samar College growth and milestones:\n"
                           "The college opened its doors and expanded as recorded in CURRENT_HISTORY_ACCOUNT.",
                           "history", section_heading="Historical Account")
        self.install([current])
        history = [{"role": "user", "content": "enrollment requirements"},
                   {"role": "assistant", "content": "3. Pay OLD_ENROLLMENT_PAYMENT."},
                   {"role": "user", "content": "history of Samar College"},
                   {"role": "assistant", "content": "History of Samar College:\n1. Founding\n2. Leadership\n3. Growth and milestones\nChoose a number."}]
        companion = "history of Samar College growth and milestones"
        model = self.ns["query_optimizer_model"].invoke
        model.side_effect = None
        model.return_value = SimpleNamespace(content=companion)
        result = self.chat("3", history)
        model.assert_called_once_with(self.optimizer.optimizer_messages("3", history))
        self.assertEqual(self.retrieval.call_args.args[0], "3")
        self.assertEqual(self.retrieval.call_args.kwargs["user_question"], "3")
        self.assertEqual(self.ranking.call_args.args[0], "3")
        self.assertIn(companion, self.ranking.call_args.kwargs["queries"])
        self.assertEqual(result["messages_to_llm"][-1].content, "3")
        self.assertEqual(result["citations"], ["history"])
        self.assertEqual(result["chat_history"], history)
        prompt = result["messages_to_llm"][0].content
        self.assertIn("CURRENT_HISTORY_ACCOUNT", prompt)
        for marker in ("<accepted_offer>", "<query_interpretation>", "Ask ONLY one", "OLD_ENROLLMENT_PAYMENT"):
            self.assertNotIn(marker, prompt)

    def test_programs_offered_requires_exact_raw_case_source_job(self):
        curriculum = document("Current_Curriculum.docx", "College of River Studies: Bachelor of River Studies.",
                              "curriculum", section_heading="College of River Studies")
        catalog = document(PROGRAMS, "College of Lantern Arts: Bachelor of Lantern Arts.", "programs",
                           collection_heading="Programs Offered", section_heading="College of Lantern Arts")
        hybrid = self.install([curriculum, catalog])
        exact = {"source": {"$eq": PROGRAMS}}
        hybrid.filtered = lambda q, f: [catalog] if f == exact else [
            doc for doc in hybrid.matching(q, f) if doc is not catalog
        ]
        before = deepcopy((self.dates, self.uploads, hybrid.current))
        selected = Mock()
        result = self.retrieve(on_selection=selected, verbose=True)
        self.assertGreaterEqual(len(hybrid.baseline), 45)
        self.assertIn(catalog, result)
        self.assertIs(result[result.index(catalog)], catalog)
        hybrid.ensemble.invoke.assert_not_called()
        for leaf in (hybrid.sparse, hybrid.dense):
            self.assertTrue(any(call.kwargs == {"filter": exact} for call in leaf.invoke.call_args_list))
            self.assertTrue(any(call.kwargs == {"filter": {"source": {"$in": list(self.uploads)}}}
                                for call in leaf.invoke.call_args_list))
        self.assertEqual((self.dates, self.uploads, hybrid.current), before)
        selected.assert_called_once()
        self.assertTrue(selected.call_args.args[0].restricted)
        self.assertLessEqual(len(hybrid.calls), 2 * (len(self.uploads) + 3))
        self.assertIn("newer sources only", self.output.getvalue())
        self.assertIn(PROGRAMS, self.output.getvalue())

    def test_current_hit_after_forty_is_selected_before_ranker_cutoff(self):
        noise = [document("Library_Notice.docx", f"Library book return reminder {i}.", f"noise-{i}")
                 for i in range(48)]
        current = document("Enrollment_Update.Docx", "Enrollment requires the CURRENT_FORM online.", "current")
        self.install([*noise, current])
        chosen = Mock()
        result = self.retrieve("What are the enrollment requirements?", top_k=1, on_selection=chosen)
        fetched = self.selections.call_args.args[0]
        self.assertEqual(len(fetched), 49)
        self.assertIs(fetched[48], current)
        self.assertEqual(result, [current])
        self.assertEqual(self.ranking.call_args.args[1], [current])
        self.assertEqual(self.ranking.call_args.kwargs["max_pairs"], 40)
        self.assertEqual(self.selections.call_args.kwargs["limit"], len(fetched))
        chosen.assert_called_once()
        self.hybrid.ensemble.invoke.assert_not_called()

    def test_unrelated_modern_shs_and_services_allow_real_handbook_fallback(self):
        for source, text in (
            ("Senior_High_Programs.docx", "Senior High School departments: Science and Languages. STEM strand."),
            ("Student_Services.docx", "Student services departments: registrar, cashier, and library offices."),
        ):
            with self.subTest(source=source):
                current = document(source, text, "unrelated")
                self.install([current])
                chosen = Mock()
                result = self.retrieve(on_selection=chosen, verbose=True)
                self.assertIn(self.old[0], result)
                self.assertTrue(self.hybrid.ensemble.invoke.called)
                chosen.assert_called_once()
                selection = chosen.call_args.args[0]
                self.assertFalse(selection.restricted)
                self.assertIn(current, selection.documents)
                self.assertEqual(self.selections.call_args.kwargs["limit"], 46)
                self.assertIn("fallback/comparison search", self.output.getvalue())

    def test_empty_new_results_report_only_final_fallback_selection(self):
        self.uploads = {PROGRAMS: STAMP}
        chosen = Mock(side_effect=lambda selection: self.assertFalse(self.ranking.called))
        result = self.retrieve(on_selection=chosen)
        limit = min(self.ns["FINAL_TOP_K"], self.ns["RERANK_CANDIDATES"])
        self.assertEqual(result, self.old[:limit])
        self.assertTrue(self.hybrid.sparse.invoke.called)
        self.assertTrue(self.hybrid.dense.invoke.called)
        chosen.assert_called_once()
        self.assertFalse(chosen.call_args.args[0].restricted)
        calls = self.hybrid.calls
        fallback = next(i for i, call in enumerate(calls) if call[0] == "ensemble")
        self.assertTrue(all(call[2] is not None for call in calls[:fallback]))
        self.assertTrue(all(call[2] is None for call in calls[fallback:]))

    def test_filtered_leaf_errors_never_prepare_an_old_answer(self):
        question = "What are the enrollment requirements?"
        for failed_leaf in ("sparse", "dense"):
            with self.subTest(leaf=failed_leaf):
                current = document("Enrollment_Update.docx", "Enrollment requires CURRENT_FORM.", "current")
                self.install([current])
                error = ValueError("index service refused the filtered lookup")
                getattr(self.hybrid, failed_leaf).invoke.side_effect = error
                chosen = Mock()
                with self.assertRaisesRegex(RuntimeError, "Filtered document search is unavailable") as raised:
                    self.retrieve(question, on_selection=chosen)
                self.assertIs(raised.exception.__cause__, error)
                chosen.assert_not_called()
                history = [{"role": "assistant", "content": "OLD_ANSWER_MUST_NOT_RETURN"}]
                result = self.chat(question, history)
                self.assertEqual(result["messages_to_llm"], [])
                self.assertEqual(result["citations"], [])
                self.assertEqual(result["chat_history"], history)
                self.hybrid.ensemble.invoke.assert_not_called()
        self.ranking.assert_not_called()
        self.multi_ranking.assert_not_called()

    def test_capacity_timeouts_propagate_unfiltered_service_errors_do_not(self):
        metadata_filter = {"source": {"$eq": PROGRAMS}}
        query = "shuttle transport departure timetable route"
        for error in (self.runtime.CapacityError("busy"), TimeoutError("expired")):
            for filtered in (False, True):
                with self.subTest(error=type(error).__name__, filtered=filtered):
                    self.install([])
                    self.hybrid.sparse.invoke.side_effect = error
                    self.hybrid.ensemble.invoke.side_effect = error
                    jobs = [(query, metadata_filter)] if filtered else None
                    with self.assertRaises(type(error)) as raised:
                        self.ns["multi_query_retrieve"](query, filtered_queries=jobs, allow_spelling=False)
                    self.assertIs(raised.exception, error)
        self.install([])
        self.hybrid.ensemble.invoke.side_effect = ValueError("unfiltered service unavailable")
        self.assertEqual(self.retrieve("Where is the shuttle stop?"), [])
        self.assertIn("non-fatal", self.output.getvalue())
        self.install([])
        with self.assertRaises(TimeoutError):
            self.retrieve(request_budget=self.runtime.RequestBudget(seconds=0))
        self.hybrid.ensemble.invoke.assert_not_called()

    def test_disabled_and_failing_ranker_never_restore_old_sources(self):
        for mode in ("disabled", "scoring failure"):
            with self.subTest(mode=mode):
                current = document("Enrollment_Update.docx", "Enrollment requires CURRENT_FORM only.", "current")
                self.install([current])
                model = SimpleNamespace(predict=Mock(side_effect=RuntimeError("scorer failed")))
                with patch.object(self.reranker, "_load_model", return_value=None if mode == "disabled" else model):
                    chosen = Mock()
                    result = self.retrieve("What are the enrollment requirements?", on_selection=chosen)
                self.assertEqual(result, [current])
                chosen.assert_called_once()
                self.assertTrue(chosen.call_args.args[0].restricted)
                self.hybrid.ensemble.invoke.assert_not_called()
                self.assertEqual(model.predict.call_count, int(mode != "disabled"))

    def test_restricted_empty_or_shrunken_ranking_cannot_restore_history(self):
        current = [document("Enrollment_Update.docx", f"Enrollment step CURRENT_STEP_{i} uses an online form.",
                            f"current-{i}") for i in range(18)]
        history = [{"role": "user", "content": f"Prior enrollment question {i}"} for i in range(10)] + [
            {"role": "assistant", "content": "STALE_ASSISTANT_FACT"},
            {"role": "system", "content": "STALE_SYSTEM_SUMMARY"},
        ]
        for ranked in ([], current[:1]):
            with self.subTest(remaining=len(ranked)):
                self.install(current)
                self.selections.reset_mock()
                with patch.dict(self.ns, {"rerank": Mock(return_value=ranked)}):
                    result = self.chat("What are the enrollment requirements?", history)
                self.assertEqual(len(result["messages_to_llm"]), 2, self.output.getvalue())
                prompt = result["messages_to_llm"][0].content
                self.assertIn(self.policy.SOURCE_SELECTION_GUIDANCE, prompt)
                self.assertIn("Prior enrollment question 0", prompt)
                for marker in ("STALE_ASSISTANT_FACT", "STALE_SYSTEM_SUMMARY", "STALE_SUMMARY_FACT", "OLD_RULE_"):
                    self.assertNotIn(marker, prompt)
                self.assertEqual(result["citations"], [doc.metadata["chunk_id"] for doc in ranked])
                self.assertEqual(result["chat_history"], history)
                self.assertEqual(self.selections.call_args.args[0], current,
                                 "Do not reclassify the reduced ranked pool")
                self.hybrid.ensemble.invoke.assert_not_called()
        self.summary.assert_not_called()

    def test_graph_uses_one_snapshot_and_caps_only_final_context(self):
        limit = min(self.ns["FINAL_TOP_K"], self.ns["RERANK_CANDIDATES"])
        current = [document("Enrollment_Update.docx", f"Enrollment requires CURRENT_FORM_{i}.", f"current-{i}")
                   for i in range(limit + 7)]
        self.install(current)
        self.dates[current[0].metadata["source"].lower()] = {
            "filename": current[0].metadata["source"], "effective_date": "2026-06-01",
        }
        before = deepcopy((self.dates, self.uploads, current))
        with patch.object(self.policy, "newer_source_names", wraps=self.policy.newer_source_names) as discover:
            result = self.chat("What are the enrollment requirements?")
        self.ns["get_doc_dates"].assert_called_once_with()
        self.ns["get_file_upload_dates"].assert_called_once_with()
        self.assertIs(discover.call_args.kwargs["dates"], self.dates)
        self.assertIs(discover.call_args.kwargs["uploads"], self.uploads)
        for call in self.selections.call_args_list:
            self.assertIs(call.kwargs["dates"], self.dates)
            self.assertIs(call.kwargs["uploads"], self.uploads)
        for name in ("source_priority", "freshness_block"):
            self.assertIs(self.ns[name].call_args.kwargs["dates"], self.dates)
        self.assertEqual(len(self.ranking.call_args.args[1]), len(current))
        self.assertEqual(self.ranking.call_args.kwargs["top_k"], self.ns["RERANK_CANDIDATES"])
        self.assertEqual(result["citations"], [doc.metadata["chunk_id"] for doc in current[:limit]])
        prompt = result["messages_to_llm"][0].content
        self.assertIn(f"CURRENT_FORM_{limit - 1}.", prompt)
        self.assertNotIn(f"CURRENT_FORM_{limit}.", prompt)
        self.assertEqual((self.dates, self.uploads, current), before)
        self.hybrid.ensemble.invoke.assert_not_called()

    def test_explicit_unit_reaches_handbook_without_importing_clas_history(self):
        old = document(BASE, "College of Arts and Sciences (CAS)\nCAS_PROFILE_FROM_2024.", "old-cas", page=31,
                       section_heading="College of Arts and Sciences")
        other = document(PROGRAMS, "College of Liberal Arts and Sciences (CLAS)\nBachelor of Arts in Sociology.",
                         "new-clas", section_heading="College of Liberal Arts and Sciences")
        self.install([other], baseline=[*self.old, other, old])
        history = [{"role": "assistant", "content": "WRONG_CLAS_FROM_HISTORY: only Sociology is offered."}] * 11
        result = self.chat("college of arts and science", history)
        self.assertEqual(result["citations"], ["old-cas"])
        system = result["messages_to_llm"][0].content
        self.assertIn("CAS_PROFILE_FROM_2024", system)
        self.assertIn("<unit_selection>", system)
        self.assertIn("<dated_unit_evidence>", system)
        self.assertIn("Begin the visible answer", system)
        self.assertNotIn("new-clas", system)
        self.assertNotIn("WRONG_CLAS_FROM_HISTORY", system)
        self.assertNotIn(self.policy.SOURCE_SELECTION_GUIDANCE, system)
        self.assertTrue(self.hybrid.ensemble.invoke.called)
        self.summary.assert_not_called()

    def test_newer_exact_unit_still_wins_and_explicit_clas_uses_its_catalog(self):
        old = document(BASE, "College of Arts and Sciences (CAS)\nOLD_CAS_PROFILE.", "old-cas")
        cas = document(PROGRAMS, "College of Arts and Sciences (CAS)\nCURRENT_CAS_PROFILE.", "new-cas")
        clas = document("Other_Catalog.docx", "College of Liberal Arts and Sciences (CLAS)\nCURRENT_CLAS_PROFILE.",
                        "new-clas")
        for question, expected in (("College of Arts and Sciences", "new-cas"),
                                   ("College of Liberal Arts and Sciences", "new-clas")):
            with self.subTest(question=question):
                self.install([cas, clas], baseline=[old, cas, clas])
                result = self.chat(question)
                self.assertEqual(result["citations"], [expected])
                self.assertNotIn("<dated_unit_evidence>", result["messages_to_llm"][0].content)
                self.hybrid.ensemble.invoke.assert_not_called()

    def test_dean_roster_reaches_context_instead_of_only_a_role_description(self):
        profile = document(BASE, "CAS is headed by a College Dean under the Vice-President.", "cas-profile", page=31,
                           section_heading="College of Arts and Sciences")
        text = "AVERY EXAMPLE, PhD Vice-President for Academic Affairs / Dean College of Arts and Sciences"
        roster = document(BASE, text, "cas-roster", page=146, section_heading="Academic Council")
        adjacent = document(BASE, "National laws\n" + text, "adjacent-copy", page=144, section_heading="National Laws")
        other = document(PROGRAMS, "JORDAN EXAMPLE, PhD Dean, College of Liberal Arts and Sciences", "clas-roster",
                         section_heading="Academic Council")
        self.install([other], baseline=[profile, adjacent, roster, other])
        result = self.chat("dean of cas", [{"role": "assistant", "content": "OLD_MISSING_NAME_REPLY"}])
        self.assertIn("cas-roster", result["citations"])
        self.assertNotIn("clas-roster", result["citations"])
        self.assertNotIn("adjacent-copy", result["citations"])
        system = result["messages_to_llm"][0].content
        self.assertIn(text, system)
        self.assertIn("146", system)
        self.assertIn("<dated_unit_evidence>", system)
        self.assertNotIn("OLD_MISSING_NAME_REPLY", system)
        self.assertEqual(roster.page_content, text)

    def test_latest_general_question_controls_scope_not_a_history_enriched_rewrite(self):
        current = document(PROGRAMS, "College of Liberal Arts and Sciences\nBachelor of Arts in Sociology.",
                           "new-clas", collection_heading="Programs Offered")
        self.install([current])
        selected = Mock()
        result = self.retrieve("College of Arts and Sciences college department",
                               user_question="college department", on_selection=selected)
        self.assertEqual(result, [current])
        self.assertEqual(selected.call_args.args[0].requested_unit, "")
        self.assertEqual(self.selections.call_args.args[1][0], "college department")

        history = [{"role": "user", "content": "PREVIOUS_SINGLE_UNIT_ONLY: College of Arts and Sciences"},
                   {"role": "assistant", "content": "PREVIOUS_SINGLE_UNIT_REPLY"}]
        result = self.chat("college department", history)
        system = result["messages_to_llm"][0].content
        self.assertIn("<department_list_request>", system)
        self.assertNotIn("PREVIOUS_SINGLE_UNIT_ONLY", system)
        self.assertNotIn("PREVIOUS_SINGLE_UNIT_REPLY", system)
        self.assertEqual(history[0]["content"], "PREVIOUS_SINGLE_UNIT_ONLY: College of Arts and Sciences")

    def test_confirmed_current_offerings_after_cas_reaches_the_programs_file(self):
        source = "Samar_college_Inc_Programs_offered.docx"
        current = [document(source, f"{unit}\n{degree}", key,
                            collection_heading="Programs Offered", section_heading=unit)
                   for unit, degree, key in (
                       ("College of Information Technology and Allied Sciences", "Bachelor of Science in Information Technology", "bsit"),
                       ("College of Liberal Arts and Sciences", "Bachelor of Arts in Communication", "communication"),
                   )]
        curriculum = document("Curriculum.docx", "First year subject timetable and laboratory hours.", "curriculum")
        old = document(BASE, "College of Arts and Sciences (CAS)\nOLD_CAS_PROFILE.", "cas",
                       section_heading="College of Arts and Sciences")
        training = document(BASE, "SCTI Training Offerings\nCookery NC II\nOLD_TRAINING_ENTRY", "training")
        hybrid = self.install([curriculum, *current], baseline=[old, training])
        exact = {"source": {"$eq": source}}
        hybrid.filtered = lambda query, filters: current if filters == exact else [curriculum]
        first = self.chat("cas")
        self.assertEqual(first["citations"], ["cas"])
        hybrid.ensemble.invoke.reset_mock()
        history = [{"role": "user", "content": "cas"}, {"role": "assistant", "content":
                   "OLD_CAS_REPLY reflects the 2024 edition only and is not confirmed current offerings."}]
        question = "confirmed current offerings"
        self.ns["query_optimizer_model"].invoke.side_effect = lambda messages: SimpleNamespace(content=question + " CAS")
        result = self.chat(question, history)
        self.assertEqual(set(result["citations"]), {"bsit", "communication"}, self.output.getvalue())
        prompt = result["messages_to_llm"][0].content
        self.assertIn(source, prompt)
        self.assertIn("Bachelor of Science in Information Technology", prompt)
        self.assertIn("Bachelor of Arts in Communication", prompt)
        self.assertIn("<program_list_request>", prompt)
        self.assertNotIn("<department_list_request>", prompt)
        for stale in (BASE, "OLD_CAS_REPLY", "OLD_TRAINING_ENTRY", "OLD_CAS_PROFILE"):
            self.assertNotIn(stale, prompt)
        self.assertEqual(result["messages_to_llm"][-1].content, question)
        self.assertEqual(self.ranking.call_args.args[0], question)
        self.assertTrue(any(call.kwargs["filter"] == exact for call in hybrid.dense.invoke.call_args_list))
        hybrid.ensemble.invoke.assert_not_called()

    def test_affirmative_followup_uses_model_companion_without_offer_guidance_or_old_facts(self):
        current = [document(PROGRAMS, unit + "\n" + degree, key, section_heading=unit,
                            collection_heading="Programs Offered") for unit, degree, key in (
            ("College of Information Technology and Allied Sciences", "Bachelor of Science in Information Technology", "citas"),
            ("College of Business and Management", "Bachelor of Science in Business Administration", "cbm"),
        )]
        old = document(BASE, "College of Arts and Sciences\nOLD_CAS_PROGRAM", "cas",
                       section_heading="College of Arts and Sciences")
        hybrid = self.install(current, baseline=[old])
        history = [{"role": "user", "content": "cas"}, {"role": "assistant", "content":
                   "OLD_CAS_CLAIM is from 2024. If you'd like, I can share details on the currently active colleges instead."}]
        before = deepcopy(history)
        companion = "currently active colleges"
        self.ns["query_optimizer_model"].invoke.side_effect = lambda messages: SimpleNamespace(content=companion)
        for reply in ("yes do it", "yes, please", "YES"):
            with self.subTest(reply=reply):
                self.ns["query_optimizer_model"].invoke.reset_mock()
                result = self.chat(reply, history)
                self.assertEqual(set(result["citations"]), {"citas", "cbm"}, self.output.getvalue())
                prompt = result["messages_to_llm"][0].content
                self.assertNotIn("<accepted_offer>", prompt)
                self.assertNotIn("<query_interpretation>", prompt)
                self.assertNotIn("<department_list_request>", prompt)
                self.assertIn(self.policy.SOURCE_SELECTION_GUIDANCE, prompt)
                self.assertNotIn("OLD_CAS_CLAIM", prompt)
                self.assertNotIn("OLD_CAS_PROGRAM", prompt)
                self.assertNotIn(BASE, prompt)
                self.assertEqual(result["messages_to_llm"][-1].content, reply)
                self.assertEqual(self.retrieval.call_args.args[0], reply)
                self.assertEqual(self.retrieval.call_args.kwargs["user_question"], reply)
                self.assertEqual(self.ranking.call_args.args[0], reply)
                self.assertIn(companion, self.ranking.call_args.kwargs["queries"])
                self.assertEqual(self.selections.call_args.args[1][0], reply)
                self.ns["query_optimizer_model"].invoke.assert_called_once_with(
                    self.optimizer.optimizer_messages(reply, history),
                )
                self.assertEqual(result["chat_history"], before)
        self.summary.assert_not_called()
        hybrid.ensemble.invoke.assert_not_called()
        self.assertEqual(history, before)

    def test_explicit_history_reply_retrieves_history_not_the_previous_programs(self):
        historical = document(BASE, "The school opened its doors as described in this historical account of its founding.",
                              "history", section_heading="Historical Account")
        self.install([], baseline=[historical])
        history = [{"role": "user", "content": "current offerings"}, {"role": "assistant", "content":
                   "OLD_PROGRAM_CLAIM. Would you like me to explain the history of Samar College?"}]
        question = "explain the history of Samar College"
        self.ns["query_optimizer_model"].invoke.side_effect = lambda messages: SimpleNamespace(content="Samar College historical account")
        result = self.chat(question, history)
        self.assertEqual(result["citations"], ["history"], self.output.getvalue())
        prompt = result["messages_to_llm"][0].content
        self.assertNotIn("<accepted_offer>", prompt)
        self.assertIn("<institution_history_request>", prompt)
        self.assertNotIn("OLD_PROGRAM_CLAIM", prompt)
        self.assertNotIn(self.policy.SOURCE_SELECTION_GUIDANCE, prompt)
        self.assertEqual(result["messages_to_llm"][-1].content, question)
        self.assertEqual(self.ranking.call_args.args[0], question)
        self.assertEqual(self.retrieval.call_args.kwargs["user_question"], question)

    def test_explicit_current_college_reply_does_not_make_an_old_list_current(self):
        old = document(BASE, "College of Arts and Sciences\nOLD_COLLEGE_LIST", "old-college")
        self.install([], baseline=[old])
        history = [{"role": "user", "content": "cas"}, {"role": "assistant", "content":
                   "OLD_CAS_CLAIM. If you'd like, I can share details on the currently active colleges instead."}]
        question = "list the currently active colleges"
        result = self.chat(question, history)
        self.assertEqual(result["citations"], [])
        prompt = result["messages_to_llm"][0].content
        self.assertNotIn("<accepted_offer>", prompt)
        self.assertIn("<department_list_request>", prompt)
        self.assertNotIn("OLD_CAS_CLAIM", prompt)
        self.assertNotIn("OLD_COLLEGE_LIST", prompt)
        self.assertNotIn(self.policy.SOURCE_SELECTION_GUIDANCE, prompt)
        self.assertEqual(result["messages_to_llm"][-1].content, question)

    def test_ambiguous_offer_echo_uses_natural_retrieval_without_forced_clarification(self):
        self.install([])
        history = [{"role": "assistant", "content":
                    "I can list currently active colleges or explain the enrollment requirements."}]
        result = self.chat("yes do it", history)
        self.assertEqual(result["citations"], [doc.metadata["chunk_id"] for doc in self.old[:self.ns["FINAL_TOP_K"]]])
        prompt = result["messages_to_llm"][0].content
        self.assertNotIn("Ask ONLY one", prompt)
        self.assertNotIn("<accepted_offer>", prompt)
        self.assertNotIn("<department_list_request>", prompt)
        self.assertEqual(result["messages_to_llm"][-1].content, "yes do it")
        self.assertEqual(self.ranking.call_args.args[0], "yes do it")
        self.assertEqual(self.ranking.call_args.kwargs["queries"], [])
        self.assertTrue(self.hybrid.ensemble.invoke.called)
        self.assertEqual({call.args[0] for call in self.hybrid.ensemble.invoke.call_args_list},
                         set(self.ns["expand_queries"]("yes do it")))
        self.hybrid.dense.invoke.assert_not_called()
        self.ns["query_optimizer_model"].invoke.assert_called_once_with(
            self.optimizer.optimizer_messages("yes do it", history),
        )

    def test_missing_current_catalog_does_not_prepare_a_confirmed_2024_answer(self):
        old = document(BASE, "SCTI Training Offerings\nOLD_TRAINING_ENTRY", "training")
        self.install([], baseline=[old])
        history = [{"role": "user", "content": "cas"},
                   {"role": "assistant", "content": "OLD_OFFERING_IN_HISTORY"}] * 6
        self.uploads = {PROGRAMS: STAMP}
        result = self.chat("confirmed current offerings", history)
        self.assertEqual(result["citations"], [])
        prompt = result["messages_to_llm"][0].content
        self.assertIn("<program_list_request>", prompt)
        self.assertNotIn(BASE, prompt)
        self.assertNotIn("OLD_TRAINING_ENTRY", prompt)
        self.assertNotIn("OLD_OFFERING_IN_HISTORY", prompt)
        self.assertNotIn(self.policy.SOURCE_SELECTION_GUIDANCE, prompt)
        self.summary.assert_not_called()

    def test_institution_history_searches_the_handbook_even_when_current_hits_contain_history(self):
        history_doc = document(BASE, "THE HISTORICAL ACCOUNT OF SAMAR COLLEGE\n"
                               "Our school opened its doors on July 1, 1949 under the name of Samar Junior College.",
                               "history", page=8, section_heading="THE HISTORICAL ACCOUNT OF SAMAR COLLEGE")
        course = document("Curriculum.docx", "Course Code | Subject | Units\nGE 2 | Readings in Philippine History | 3\n"
                          "General education subjects offered in the first year.", "history-class",
                          collection_heading="CITAS Curriculum", section_heading="First Year")
        hybrid = self.install([course], baseline=[course])
        exact = {"source": {"$eq": BASE}}
        hybrid.filtered = lambda query, filters: [history_doc] if filters == exact else [course]
        self.uploads = {"Curriculum.docx": STAMP}
        question = "history of samar college"
        past = [{"role": "user", "content": "confirmed current offerings"},
                {"role": "assistant", "content": "ONLY_CURRENT_PROGRAMS_CLAIM: CITAS and CBM curricula."}] * 6
        self.ns["query_optimizer_model"].invoke.side_effect = lambda messages: SimpleNamespace(content=question + " current offerings CITAS")
        result = self.chat(question, past)
        self.assertEqual(result["citations"], ["history"], self.output.getvalue())
        prompt = result["messages_to_llm"][0].content
        self.assertIn("July 1, 1949", prompt)
        self.assertIn("Samar Junior College", prompt)
        self.assertIn("<institution_history_request>", prompt)
        self.assertNotIn(self.policy.SOURCE_SELECTION_GUIDANCE, prompt)
        self.assertNotIn("Readings in Philippine History", prompt)
        self.assertNotIn("ONLY_CURRENT_PROGRAMS_CLAIM", prompt)
        self.assertEqual(result["messages_to_llm"][-1].content, question)
        self.assertEqual(self.ranking.call_args.args[0], question)
        self.assertTrue(any(call.kwargs["filter"] == exact for call in hybrid.dense.invoke.call_args_list))
        self.assertTrue(hybrid.ensemble.invoke.called, "Historical recall is not limited to newer sources")
        self.summary.assert_not_called()

    def test_missing_history_does_not_prepare_an_entire_knowledge_base_absence_claim(self):
        unrelated = document("Curriculum.docx", "Readings in Philippine History is a first year subject.",
                             "history-subject", collection_heading="Curriculum")
        self.install([unrelated], baseline=[unrelated])
        result = self.chat("history of samar college")
        self.assertEqual(result["citations"], [], self.output.getvalue())
        prompt = result["messages_to_llm"][0].content
        self.assertIn("<institution_history_request>", prompt)
        self.assertIn("retrieved", prompt)
        self.assertNotIn("Readings in Philippine History", prompt)
        self.assertNotIn(self.policy.SOURCE_SELECTION_GUIDANCE, prompt)

    def test_history_followup_companion_does_not_replace_natural_request_text(self):
        history_doc = document(BASE, "The college was established as documented in this historical account of its founding.",
                               "founding", section_heading="Historical Account")
        self.install([history_doc], baseline=[history_doc])
        question = "when was it founded?"
        companion = "when was Samar College founded?"
        self.ns["query_optimizer_model"].invoke.side_effect = lambda messages: SimpleNamespace(content=companion)
        past = [{"role": "user", "content": "tell me about Samar College"},
                {"role": "assistant", "content": "OLD_NO_HISTORY_CLAIM"}]
        result = self.chat(question, past)
        self.assertEqual(result["citations"], ["founding"], self.output.getvalue())
        prompt = result["messages_to_llm"][0].content
        self.assertEqual(self.retrieval.call_args.args[0], question)
        self.assertEqual(self.retrieval.call_args.kwargs["user_question"], question)
        self.assertEqual(self.ranking.call_args.args[0], question)
        self.assertIn(companion, self.ranking.call_args.kwargs["queries"])
        self.assertIn(question, self.selections.call_args.args[1])
        self.assertEqual(result["messages_to_llm"][-1].content, question)
        self.ns["query_optimizer_model"].invoke.assert_called_once_with(
            self.optimizer.optimizer_messages(question, past),
        )
        with self.subTest(boundary="natural request text"):
            self.assertEqual(self.ns["authority_block"].call_args.args[:2], (question, question))
        with self.subTest(boundary="user antecedent"):
            self.assertIn("tell me about Samar College", prompt)
        with self.subTest(boundary="stale assistant facts"):
            self.assertNotIn("OLD_NO_HISTORY_CLAIM", prompt)

    def test_failed_reference_search_cannot_claim_that_history_is_missing(self):
        self.install([])
        self.hybrid.sparse.invoke.side_effect = RuntimeError("service unavailable")
        with self.assertRaisesRegex(RuntimeError, "Filtered document search is unavailable"):
            self.retrieve("history of samar college")
        self.ranking.assert_not_called()

    def test_reference_query_keeps_the_resolved_named_unit(self):
        old = document(BASE, "College of Arts and Sciences (CAS)\nPrograms offered: OLD_CAS_FIELD.", "old-cas")
        current = document(PROGRAMS, "College of Liberal Arts and Sciences\nBachelor of Arts in Sociology.",
                           "new-clas", collection_heading="Programs Offered")
        self.install([current], baseline=[old, current])
        result = self.retrieve("unit College of Arts and Sciences: what programs does it offer?",
                               user_question="what programs does it offer?")
        self.assertEqual(result, [old])

    def test_dates_only_raw_filenames_and_unknown_dates_are_honest(self):
        raw = "Enrollment_Guide (Revised).DOCX"
        current = document(raw, "Enrollment requires the CURRENT_FORM online.", "current")
        for effective in ("2026-06-01", None, "not-a-date"):
            with self.subTest(effective=effective):
                self.install([current], baseline=[self.old[0], current])
                self.uploads = {}
                self.dates[raw.lower()] = {"filename": raw, "effective_date": effective}
                chosen = Mock()
                result = self.retrieve("What are the enrollment requirements?", on_selection=chosen)
                chosen.assert_called_once()
                selection = chosen.call_args.args[0]
                self.assertEqual(selection.restricted, effective == "2026-06-01")
                if effective == "2026-06-01":
                    self.assertEqual(result, [current])
                    self.assertTrue(self.hybrid.sparse.invoke.called)
                    self.assertTrue(all(call.kwargs["filter"] == {"source": {"$in": [raw]}}
                                        for call in self.hybrid.sparse.invoke.call_args_list))
                    self.hybrid.ensemble.invoke.assert_not_called()
                else:
                    self.assertEqual(result, [self.old[0], current])
                    self.hybrid.sparse.invoke.assert_not_called()
                    self.assertTrue(self.hybrid.ensemble.invoke.called)

    def test_historical_queries_preserve_requested_baseline_and_deduplicate(self):
        current = document("Enrollment_Update.docx", "Enrollment now requires CURRENT_FORM.", "current")
        for question in ("What were enrollment requirements in 2024?",
                         "Compare the 2024 and 2026 enrollment requirements"):
            with self.subTest(question=question):
                self.install([current], baseline=[self.old[0], current])
                chosen = Mock()
                result = self.retrieve(question, on_selection=chosen)
                self.assertEqual(result, [self.old[0], current])
                self.assertEqual(sum(doc is current for doc in self.selections.call_args.args[0]), 1)
                chosen.assert_called_once()
                self.assertFalse(chosen.call_args.args[0].restricted)
                self.assertTrue(self.hybrid.ensemble.invoke.called)
        history = [{"role": "assistant", "content": "Historical answer retained for comparison."}]
        result = self.chat("What were enrollment requirements in 2024?", history)
        self.assertIn(history[0]["content"], result["messages_to_llm"][0].content)
        self.assertNotIn(self.policy.SOURCE_SELECTION_GUIDANCE, result["messages_to_llm"][0].content)

    def test_distinct_catalog_units_survive_scoring_and_context_limits(self):
        current = [document(PROGRAMS, f"College of River Studies: Bachelor of River Studies, module {i}.",
                            f"river-{i}", section_heading="College of River Studies") for i in range(48)]
        for name in ("Lantern Arts", "Cloud Crafts"):
            current.append(document(PROGRAMS, f"College of {name}: Bachelor of {name}.", name,
                                    collection_heading="Programs Offered", section_heading=f"College of {name}"))
        self.install(current)
        predict = Mock(side_effect=lambda pairs, **kwargs: [1.0] * len(pairs))
        with patch.object(self.reranker, "_load_model", return_value=SimpleNamespace(predict=predict)):
            result = self.chat("List the college departments")
        self.assertEqual(len(result["citations"]), min(
            len(current), self.ns["FINAL_TOP_K"], self.ns["RERANK_CANDIDATES"],
        ))
        self.assertTrue({"river-0", "Lantern Arts", "Cloud Crafts"} <= set(result["citations"]))
        before_scoring = self.ranking.call_args.args[1]
        self.assertEqual([doc.metadata["chunk_id"] for doc in before_scoring[:3]],
                         ["river-0", "Lantern Arts", "Cloud Crafts"])
        passages = [text for query, text in predict.call_args.args[0]]
        self.assertTrue(any("Bachelor of Lantern Arts" in text for text in passages))
        self.assertTrue(any("Bachelor of Cloud Crafts" in text for text in passages))
        self.assertEqual(len(self.selections.call_args.args[0]), 50)
        self.hybrid.ensemble.invoke.assert_not_called()

    def test_complementary_sources_share_budget_without_mutating_selection(self):
        many = [document("Admission_Update.docx", f"Enrollment uses CURRENT_STEP_{i}.", f"step-{i}")
                for i in range(48)]
        small = document("Registrar_Review.docx", "Enrollment requires CURRENT_REVIEW of the application.", "review")
        current = [*many, small]
        self.install(current)
        before = deepcopy((current, self.dates, self.uploads))
        chosen = Mock()
        result = self.retrieve("What are the enrollment requirements?", on_selection=chosen)
        self.assertEqual(result[:2], [many[0], small])
        chosen.assert_called_once()
        selected = chosen.call_args.args[0]
        self.assertTrue(selected.restricted)
        self.assertEqual(selected.documents, current)
        self.assertEqual(len(selected.documents), 49)
        self.assertTrue(all(actual is expected for actual, expected in zip(selected.documents, current)))
        self.assertEqual((current, self.dates, self.uploads), before)
        self.hybrid.ensemble.invoke.assert_not_called()

    def test_catalog_companions_reach_ranking_without_granting_relevance(self):
        for question in (QUESTION, QUESTION + " and the college units"):
            with self.subTest(question=question):
                shs = document("Senior_High_Programs.docx", "Senior High School programs offered: STEM strand.",
                               "shs", collection_heading="Programs Offered", section_heading="Senior High School")
                self.install([shs], baseline=[self.old[0]])
                companions = self.policy.department_catalog_queries(question)
                self.assertTrue(companions)
                self.assertLessEqual(len(companions), 2)
                self.hybrid.filtered = lambda q, f, probes=companions, doc=shs: [doc] if q in probes else []
                self.selections.reset_mock()
                predict = Mock(side_effect=lambda pairs, **kwargs: [1.0] * len(pairs))
                with patch.object(self.reranker, "_load_model", return_value=SimpleNamespace(predict=predict)):
                    chosen = Mock()
                    self.retrieve(question, on_selection=chosen)
                ranked_queries = {q for q, text in predict.call_args.args[0]}
                self.assertTrue(set(companions) <= ranked_queries)
                self.assertIn(question, ranked_queries)
                for call in self.selections.call_args_list:
                    self.assertIn(question, call.args[1])
                    self.assertTrue(set(companions).isdisjoint(call.args[1]))
                self.assertFalse(chosen.call_args.args[0].restricted)
                self.assertTrue(self.hybrid.ensemble.invoke.called)

    def test_selection_receives_natural_phrasings_late_companion_aspects_and_language(self):
        current = document("Enrollment_Update.docx", "Enrollment application requires CURRENT_FORM and fees.", "current")
        self.install([current])
        original = "Pila an bayad sa enrollment and what are the enrollment requirements?"
        query, late = "enrollment fees", "enrollment application requirements"
        rewrite = Mock(return_value=late)
        self.retrieve(query, user_question=original, late_query=rewrite)
        expected = [original, query, late, *self.ns["split_question"](original),
                    *self.ns["language_variants"](original)]
        self.assertGreater(len(expected), 3)
        self.assertTrue(self.ns["language_variants"](original))
        for call in self.selections.call_args_list:
            self.assertEqual(call.args[1], expected)
        rewrite.assert_called_once_with()
        self.assertTrue(self.multi_ranking.called)
        self.hybrid.ensemble.invoke.assert_not_called()

    def test_concurrent_requests_do_not_leak_filters_or_mutate_settings(self):
        current = [document(f"Enrollment_{name}.DOCX", f"Enrollment requires CURRENT_{name}_FORM.", name)
                   for name in ("River", "Lantern")]
        hybrid = self.install(current)
        barrier = threading.Barrier(2)

        def synchronized(query, metadata_filter):
            barrier.wait(timeout=5)
            return hybrid.matching(query, metadata_filter)

        hybrid.filtered = synchronized
        callbacks = [Mock(), Mock()]
        snapshots = [{doc.metadata["source"]: STAMP} for doc in current]
        before = deepcopy(snapshots)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as requests:
            futures = [requests.submit(self.retrieve, "What are the enrollment requirements?",
                                       dates=self.dates, uploads=uploads, on_selection=callback)
                       for uploads, callback in zip(snapshots, callbacks)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results, [[current[0]], [current[1]]])
        self.assertEqual(snapshots, before)
        for doc, callback in zip(current, callbacks):
            callback.assert_called_once()
            self.assertEqual(callback.call_args.args[0].documents, [doc])
        hybrid.ensemble.invoke.assert_not_called()
        self.ns["multi_query_retrieve"]("Where is the shuttle stop?", allow_spelling=False)
        self.assertTrue(all(call.kwargs == {} for call in hybrid.ensemble.invoke.call_args_list))
        self.assertEqual(hybrid.ensemble.weights, (0.45, 0.55))
        for leaf, alpha in ((hybrid.sparse, 0.0), (hybrid.dense, 1.0)):
            self.assertEqual((leaf.top_k, leaf.alpha, leaf.filter), (30, alpha, None))
            self.assertEqual(Counter(tuple(call.kwargs["filter"]["source"]["$in"])
                                     for call in leaf.invoke.call_args_list),
                             Counter((doc.metadata["source"],) for doc in current))

    def test_explicit_jobs_replace_expansion_with_two_inflight_and_deadline(self):
        docs = [document(f"Current_{i}.DOCX", f"Enrollment CURRENT_FORM_{i}.", str(i)) for i in range(6)]
        hybrid = self.install(docs)
        jobs = [(f"probe-{i}", {"source": {"$eq": doc.metadata["source"]}}) for i, doc in enumerate(docs)]
        before = deepcopy(jobs)
        barrier, lock = threading.Barrier(2), threading.Lock()
        active = peak = 0

        def counted(query, metadata_filter):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                return hybrid.matching(query, metadata_filter)
            finally:
                with lock:
                    active -= 1

        hybrid.filtered = counted
        extras = ["this unfiltered probe must not run"]
        result = self.ns["multi_query_retrieve"](QUESTION, extra_queries=extras, filtered_queries=jobs,
                                                 timeout=7, allow_spelling=False)
        self.assertEqual(result, docs)
        self.assertEqual(peak, 2)
        self.assertEqual(self.maps.call_args.kwargs, {"timeout": 7, "in_flight": 2})
        self.assertEqual(jobs, before)
        self.assertEqual(extras, ["this unfiltered probe must not run"])
        self.assertEqual(hybrid.ensemble.weighted_reciprocal_rank.call_count, len(jobs))
        self.assertTrue(all(len(call.args[0]) == 2 for call in hybrid.ensemble.weighted_reciprocal_rank.call_args_list))
        self.assertEqual(len(hybrid.calls), 2 * len(jobs))
        hybrid.ensemble.invoke.assert_not_called()
        self.assertEqual(self.ns["multi_query_retrieve"](QUESTION, filtered_queries=[]), [])
        self.assertEqual(len(hybrid.calls), 2 * len(jobs))

    def test_ordinary_empty_snapshots_preserve_unfiltered_order_and_inputs(self):
        docs = [document(f"Notes_{i % 2}.docx", f"Shuttle stop location NOTE_{i}.", str(i)) for i in range(4)]
        hybrid = self.install([], baseline=docs)
        question = "Where is the shuttle stop?"
        probes = self.ns["expand_queries"](question, allow_spelling=False)
        self.assertEqual(len(probes), 3)
        answers = dict(zip(probes, ([docs[0], docs[1]], [docs[2], docs[0]], [docs[1], docs[3]])))
        hybrid.ensemble.invoke.side_effect = lambda q: list(answers[q])
        before = deepcopy(docs)
        chosen = Mock()
        result = self.retrieve(question, dates={}, uploads={}, top_k=4, on_selection=chosen)
        self.assertEqual(result, [docs[0], docs[2], docs[1], docs[3]])
        self.assertEqual(docs, before)
        self.assertEqual(Counter(call.args[0] for call in hybrid.ensemble.invoke.call_args_list), Counter(probes))
        self.assertTrue(all(call.kwargs == {} for call in hybrid.ensemble.invoke.call_args_list))
        self.assertEqual(self.ranking.call_args.kwargs["queries"], [])
        self.assertFalse(chosen.call_args.args[0].restricted)
        chosen.assert_called_once()
        hybrid.sparse.invoke.assert_not_called()
        hybrid.dense.invoke.assert_not_called()
        self.ns["get_doc_dates"].assert_not_called()
        self.ns["get_file_upload_dates"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
