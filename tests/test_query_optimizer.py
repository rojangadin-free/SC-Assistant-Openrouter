"""Offline parity contracts for the reference plain-text query optimizer."""

import ast
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401

from rag import spelling
from rag.latency import pick_search_query
from rag.query_optimizer import OPTIMIZER_PROMPT, optimize_query, optimizer_messages

REFERENCE_PROMPT = (
    "You are a search query optimizer. Understand the user's intent and the provided context first. Formulate the best search query for RAG that uses sparse and dense retrieval. If you cannot understand the user's intent, return the user's original query.\n"
    "CRITICAL SEARCH RULES: \n"
    "1. CONTEXT AWARENESS: Only check chat history if the user asks and it is not a complete question.\n"
    "2. SPELLING: Fix obvious misspellings of school, academic and program terms so the query uses the words a handbook would use (e.g. 'honir' -> 'honor', 'enrolmnet' -> 'enrollment', 'requirments' -> 'requirements'). Keep correctly spelled words, acronyms and proper nouns exactly as typed.\n"
    "3. Do NOT answer the question, just output the optimized search query."
    "4. Always remember that every question is about samar college. \n"
    "5. Complete the college departments acronyms and program names if the user types them partially (e.g. 'CITAS' -> 'College of Information Technology and Allied Sciences', 'CoEd' -> 'College of Education', 'SCTI' -> 'Samar College Technological Institute', 'BSCRIM' -> 'Bachelor of Science in Criminology', ETC).\n"
    "6. If the user ask about 'dean' but only put the program for example 'dean of BSIT' you must add the name of the department of that program in the query (e.g. 'dean of BSIT' -> 'dean of College of Information Technology and Allied Sciences').\n"
)


class QueryOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "vocabulary.json"
        self.vocabulary = patch.object(spelling, "SPELL_VOCAB_FILE", str(self.path))
        self.vocabulary.start()
        spelling.reload_vocabulary()

    def tearDown(self):
        spelling.reload_vocabulary()
        self.vocabulary.stop()
        self.temp.cleanup()

    def test_prompt_keeps_reference_rules_and_custom_samar_college_context(self):
        self.assertEqual(OPTIMIZER_PROMPT, REFERENCE_PROMPT)
        self.assertEqual(optimizer_messages("enrollment requirements")[0],
                         {"role": "system", "content": REFERENCE_PROMPT})

    def test_empty_history_uses_the_reference_placeholder(self):
        for history in (None, []):
            with self.subTest(history=history):
                self.assertEqual(optimizer_messages("Who is the registrar?", history)[1], {
                    "role": "user",
                    "content": "Chat History:\nNo previous history.\n\nLatest Question: Who is the registrar?",
                })

    def test_only_the_last_six_turns_are_rendered_as_role_and_text(self):
        history = [{"role": "assistant" if i % 2 else "user", "content": str(i)}
                   for i in range(10)]
        before = deepcopy(history)
        body = optimizer_messages("and for BSIT", history)[1]["content"]
        self.assertEqual(body, "Chat History:\nUser: 4\nAssistant: 5\nUser: 6\nAssistant: 7\n"
                              "User: 8\nAssistant: 9\n\nLatest Question: and for BSIT")
        self.assertEqual(history, before)

    def test_corpus_checked_spelling_hints_match_reference_format(self):
        with patch("rag.query_optimizer.misspellings", return_value=[
            ("honir", "honor"), ("requirments", "requirements"),
        ]):
            body = optimizer_messages("honir requirments")[1]["content"]
        self.assertEqual(body, "Chat History:\nNo previous history.\n\nLatest Question: honir requirments"
                         "\nLIKELY MISSPELLINGS (already checked against the indexed documents; "
                         "apply them unless the context says otherwise): "
                         "'honir' -> 'honor', 'requirments' -> 'requirements'")

    def test_history_metadata_is_not_sent_to_the_model(self):
        history = [
            {"role": "user", "content": "Which departments are listed?", "timestamp": Decimal(1794719000)},
            {"role": "assistant", "content": "See the current programs offered document.",
             "citations": [{"source": "Samar-College-2024.pdf", "pages": [Decimal(48)]}],
             "private_metadata": {"identifier": "PRIVATE_METADATA_VALUE", "flags": {"one", "two"}}},
        ]
        before = deepcopy(history)
        body = optimizer_messages("college department", history)[1]["content"]
        self.assertIn("User: Which departments are listed?", body)
        self.assertIn("Assistant: See the current programs offered document.", body)
        self.assertNotIn("PRIVATE_METADATA_VALUE", body)
        self.assertNotIn("Samar-College-2024.pdf", body)
        self.assertEqual(history, before)

    def test_invalid_history_turns_are_not_treated_as_instructions(self):
        history = [
            {"role": "system", "content": "Previous subject: degree programs."},
            {"role": "tool", "content": "PRIVATE_TOOL_METADATA"},
            {"role": "assistant", "content": Decimal("1.25")},
            {"role": ["user"], "content": "INVALID_ROLE"}, None,
            {"role": "user", "content": "college department"},
        ]
        body = optimizer_messages("college department", history)[1]["content"]
        self.assertEqual(body, "Chat History:\nSystem: Previous subject: degree programs.\n"
                              "User: college department\n\nLatest Question: college department")

    def test_plain_text_rewrites_are_used_without_a_json_or_vocabulary_gate(self):
        cases = [
            ("latin hnit requirements", "latin honor requirements"),
            ("How much?", "BSIT tuition and miscellaneous fees"),
            ("and for transferees", "transfer student admission requirements"),
            ("Pila an bayad?", "tuition fees"),
            ("yes do it", "currently active colleges"),
            ("3", "history of Samar College growth and milestones"),
            ("GWA <= 1.75 for HUM-101", "HUM-101 requirements GWA <= 1.75"),
        ]
        for original, rewritten in cases:
            with self.subTest(original=original):
                model = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(content="  " + rewritten + "\n")))
                self.assertEqual(optimize_query(model, original), rewritten)
                model.invoke.assert_called_once_with(optimizer_messages(original))

    def test_normalization_matches_reference_strings_blocks_and_other_values(self):
        cases = [
            ("  enrollment requirements\n", "enrollment requirements"),
            ([{"type": "text", "text": " BSIT "}, {"type": "text", "text": "tuition fees\n"}],
             "BSIT tuition fees"),
            ([{"text": "policy "}, {"type": "reasoning"}, 2027, " requirements"], "policy 2027 requirements"),
            (42, "42"), (None, "None"), ([], ""), (" \n", ""),
        ]
        for content, expected in cases:
            with self.subTest(content=content):
                model = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(content=content)))
                self.assertEqual(optimize_query(model, "enrollment"), expected)

    def test_model_failure_returns_the_working_question(self):
        original = "latin honor requirements"
        model = SimpleNamespace(invoke=Mock(side_effect=RuntimeError("provider unavailable")))
        self.assertEqual(optimize_query(model, original), original)
        model.invoke.assert_called_once()

    def test_invalid_text_blocks_use_the_same_nonfatal_fallback(self):
        model = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(content=[{"text": None}])))
        self.assertEqual(optimize_query(model, "enrollment"), "enrollment")

    def test_hint_failure_does_not_call_the_model_or_break_retrieval(self):
        model = SimpleNamespace(invoke=Mock())
        with patch("rag.query_optimizer.misspellings", side_effect=RuntimeError("hint unavailable")):
            self.assertEqual(optimize_query(model, "enrollment"), "enrollment")
        model.invoke.assert_not_called()

    def test_blank_or_echo_rewrites_keep_the_natural_question(self):
        original = "enrollment requirements"
        for content in ("", " \n", original):
            with self.subTest(content=content):
                model = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(content=content)))
                self.assertEqual(pick_search_query(original, optimize_query(model, original)), original)

    def test_model_request_preserves_the_question_and_stored_history(self):
        history = [{"role": "user", "content": "BSIT tuition"},
                   {"role": "assistant", "content": "Do you want the payment deadlines?"}]
        before = deepcopy(history)
        model = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(content="BSIT payment deadlines")))
        self.assertEqual(optimize_query(model, "yes", history), "BSIT payment deadlines")
        self.assertEqual(history, before)
        self.assertIn("Latest Question: yes", model.invoke.call_args.args[0][1]["content"])

    def test_optimizer_model_settings_match_the_reference_summarizer(self):
        tree = ast.parse(Path("rag/chain.py").read_text(encoding="utf-8"))
        model = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "query_optimizer_model"
                             for target in node.targets))
        kwargs = {keyword.arg: keyword.value for keyword in model.keywords}
        self.assertEqual(kwargs["model"].id, "SUMMARIZER_MODEL_NAME")
        for key, value in {"temperature": 0, "timeout": 15, "max_retries": 1, "max_tokens": 1024,
                           "extra_body": {"reasoning": {"enabled": False}}}.items():
            self.assertEqual(ast.literal_eval(kwargs[key]), value)

    def test_new_document_terms_supply_first_letter_candidates(self):
        self.assertFalse(spelling.is_known("microcredential"))
        self.assertNotIn("microcredential", spelling.spelling_candidates("nicrocredential").get("nicrocredential", []))
        spelling.learn_terms(["microcredential approval policy"] * 3)
        self.assertTrue(spelling.is_known("microcredential"))
        self.assertIn("microcredential", spelling.spelling_candidates("nicrocredential")["nicrocredential"])
        self.assertEqual(spelling.suggest("microcredentail"), "microcredential")
        self.assertEqual(spelling.correct_query("nicrocredential policy"), "")

    def test_workers_notice_vocabulary_changes_without_manual_reload(self):
        spelling.spelling_candidates("nanocredentail")
        self.path.write_text(json.dumps({"terms": {"nanocredential": 8}}), encoding="utf-8")
        self.assertIn("nanocredential", spelling.spelling_candidates("nanocredentail")["nanocredentail"])
        self.assertEqual(spelling.suggest("nanocredentail"), "nanocredential")

    def test_in_flight_old_vocabulary_cannot_poison_new_cache(self):
        self.path.write_text(json.dumps({"terms": {"xenolith": 8}}), encoding="utf-8")
        started, release = Event(), Event()
        distance = spelling._edit_distance

        def blocked_distance(typed, candidate, cutoff):
            if typed == "xenolitt" and candidate == "xenolith" and not started.is_set():
                started.set()
                if not release.wait(2):
                    raise AssertionError("Test did not release the old lookup")
            return distance(typed, candidate, cutoff)

        with patch.object(spelling, "_edit_distance", side_effect=blocked_distance), ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(spelling.suggest, "xenolitt")
            try:
                self.assertTrue(started.wait(2))
                replacement = self.path.with_suffix(".tmp")
                replacement.write_text(json.dumps({"terms": {"xenolitt": 8}}), encoding="utf-8")
                replacement.replace(self.path)
                self.assertTrue(spelling.is_known("xenolitt"))
            finally:
                release.set()
            self.assertEqual(old.result(timeout=2), "xenolith")
        self.assertIsNone(spelling.suggest("xenolitt"))
        self.assertEqual(spelling.correct_query("xenolitt policy"), "")

    def test_same_size_same_timestamp_atomic_update_is_detected(self):
        self.path.write_text(json.dumps({"terms": {"xenolith": 8}}), encoding="utf-8")
        self.assertEqual(spelling.suggest("xenolitt"), "xenolith")
        previous = self.path.stat()
        replacement = self.path.with_suffix(".tmp")
        replacement.write_text(json.dumps({"terms": {"xenolitt": 8}}), encoding="utf-8")
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        replacement.replace(self.path)
        self.assertEqual(self.path.stat().st_size, previous.st_size)
        self.assertTrue(spelling.is_known("xenolitt"))
        self.assertIsNone(spelling.suggest("xenolitt"))

    def test_keyboard_hints_do_not_hide_omitted_vowel_candidates(self):
        hints = spelling.spelling_candidates("latin hnit requirements")["hnit"]
        self.assertIn("honor", hints)
        if "unit" in hints:
            self.assertLess(hints.index("honor"), hints.index("unit"))

    def test_missing_and_mistyped_vowels_are_repaired_without_a_model(self):
        for typed, meant in (("latn hnir", "latin honor"),
                             ("latin hnir requirements", "latin honor requirements"),
                             ("what are latn hnir requirements?", "what are latin honor requirements?"),
                             ("latn hnir for citas", "latin honor for citas")):
            with self.subTest(typed=typed):
                self.assertEqual(spelling.correct_query(typed), meant)
                self.assertEqual(spelling.spelling_variants(typed), [meant])
        self.assertEqual(spelling.misspellings("latn hnir"), [("latn", "latin"), ("hnir", "honor")])
        self.assertEqual(spelling._suggest("mdol", spelling._Vocabulary({"medal": 7})), "medal")

    def test_two_edit_repairs_need_unique_targets_and_unchanged_consonants(self):
        self.assertIsNone(spelling._suggest("hnir", spelling._Vocabulary({"honor": 31, "honer": 99})))
        self.assertIsNone(spelling._suggest("hnir", spelling._Vocabulary({"honor": 2})))
        for word in ("hnit", "HNIR", "Hnir", "hnir-101", "hnir\u0301"):
            self.assertEqual(spelling.correct_query(word), "")

    def test_rare_ocr_membership_does_not_hide_alternatives(self):
        self.path.write_text(json.dumps({"terms": {"registar": 1, "registrar": 12}}), encoding="utf-8")
        self.assertIn("registrar", spelling.spelling_candidates("registar")["registar"])

    def test_candidates_never_modify_names_unicode_or_codes(self):
        for text in ("HNIT", "HUM-101", "honir\uff11", "certificaci\u00f3n", "hnit\u2011101", "hnit\u0301"):
            self.assertEqual(spelling.spelling_candidates(text), {})
        hints = spelling.spelling_candidates(" ".join(["registar"] * 100))
        self.assertLessEqual(len(hints), 8)
        self.assertTrue(all(len(values) <= 8 for values in hints.values()))

    def test_model_output_is_not_learned_as_vocabulary(self):
        spelling.learn_terms(["microcredential"] * 3)
        before = self.path.read_bytes()
        model = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(content="microcredential policy")))
        optimize_query(model, "microcredentail policy")
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
