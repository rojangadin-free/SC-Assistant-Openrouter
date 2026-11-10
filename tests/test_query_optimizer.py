"""Offline contracts for general interpretation, not per-question replacement rules."""

import _bootstrap  # noqa: F401

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import Path
from unittest.mock import patch

from rag import spelling
from rag.latency import should_optimize
from rag.query_optimizer import (
    OPTIMIZER_PROMPT, interpretation_guidance, optimizer_messages, parse_interpretation,
)


def response(question, *, status="corrected", query=None, clarification=""):
    return json.dumps({
        "status": status, "question": question,
        "search_query": question if query is None else query,
        "clarification": clarification,
    })


class QueryInterpretationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "vocabulary.json"
        self.vocabulary = patch.object(spelling, "SPELL_VOCAB_FILE", str(self.path))
        self.vocabulary.start()
        spelling.reload_vocabulary()

    def tearDown(self):
        self.vocabulary.stop()
        spelling.reload_vocabulary()
        self.temp.cleanup()

    def test_general_corrections_are_accepted_without_a_phrase_table(self):
        cases = [
            ("latin hnit requirements", "latin honor requirements"),
            ("latin hnir requirements", "latin honor requirements"),
            ("sign the internship contact", "sign the internship contract"),
            ("where is the xegistrar", "where is the registrar"),
            ("wat ar reqrmnts fr enrllmnt", "what are requirements for enrollment"),
            ("medcalcertficate requirements", "medical certificate requirements"),
            ("scholar ship eligiblity", "scholarship eligibility"),
            ("Magknao ang tuiton sa BSIT?", "Magkano ang tuition sa BSIT?"),
            ("LOST THE DENS", "LIST THE DEANS"),
            ("please lost the dens", "please list the deans"),
            ("show 'honir' requirements", "show 'honor' requirements"),
            ("bsit tuiton fees", "BSIT tuition fees"),
        ]
        for typed, meant in cases:
            with self.subTest(typed=typed):
                self.assertTrue(should_optimize(typed, []))
                parsed = parse_interpretation(response(meant), typed)
                self.assertEqual(parsed.question, meant)
                self.assertEqual(parsed.search_query, meant)
        self.assertFalse(hasattr(spelling, "_ACADEMIC_PHRASES"))
        self.assertFalse(hasattr(spelling, "_REAL_WORD_CONFUSIONS"))

    def test_valid_words_and_names_do_not_require_a_rewrite(self):
        cases = [
            "who is the internship contact", "i lost my id",
            "how do I keep a good mood during exams", "is tuition free for BSIT",
            "latin hint requirements", "Pila an bayad sa enrollment?",
            "certificaci\u00f3n requirements", "honir\uff11 policy",
            "latin hnit\u2011101 requirements", "latin hnit\u0301 requirements",
            "requirements for HUM-101", "where is Dr. Jos\u00e9 Santos",
        ]
        for original in cases:
            with self.subTest(original=original):
                parsed = parse_interpretation(response(original, status="unchanged"), original)
                self.assertEqual(parsed.question, original)
                self.assertEqual(parsed.status, "unchanged")

    def test_real_word_questions_are_always_interpreted(self):
        for text in ("sign the internship contact", "who is the internship contact",
                     "i lost the documents", "list the documents"):
            self.assertTrue(should_optimize(text, []))
            self.assertTrue(should_optimize(text, [{"role": "user", "content": "hi"}]))
        for text in ("", "  ", "?!"):
            self.assertFalse(should_optimize(text, []))

    def test_prompt_uses_original_input_and_bounded_corpus_hints(self):
        history = [{"role": "user", "content": str(i)} for i in range(10)]
        messages = optimizer_messages("i lost my ID", "i list my ID", history)
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["original_question"], "i lost my ID")
        self.assertEqual(payload["local_spelling_hint"], "i list my ID")
        self.assertEqual(payload["history"], history[-6:])
        self.assertEqual(len(history), 10)
        self.assertIn("fallible hints", messages[0]["content"])
        self.assertIn("every word is correctly spelled", OPTIMIZER_PROMPT)
        self.assertNotIn("latin hnit", OPTIMIZER_PROMPT.lower())

    def test_new_document_terms_supply_first_letter_candidates(self):
        self.assertFalse(spelling.is_known("microcredential"))
        self.assertNotIn("microcredential", spelling.spelling_candidates("nicrocredential").get("nicrocredential", []))
        spelling.learn_terms(["microcredential approval policy"] * 3)
        self.assertTrue(spelling.is_known("microcredential"))
        self.assertIn("microcredential", spelling.spelling_candidates("nicrocredential")["nicrocredential"])
        self.assertEqual(spelling.suggest("microcredentail"), "microcredential")
        # First-letter guesses are hints only until full-sentence interpretation.
        self.assertEqual(spelling.correct_query("nicrocredential policy"), "")

    def test_workers_notice_vocabulary_changes_without_manual_reload(self):
        spelling.spelling_candidates("nanocredentail")  # prime cached absence
        # Simulate another indexing worker publishing the existing JSON format.
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

        with patch.object(spelling, "_edit_distance", side_effect=blocked_distance):
            with ThreadPoolExecutor(max_workers=1) as pool:
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

    def test_rare_ocr_membership_does_not_hide_alternatives(self):
        self.path.write_text(json.dumps({"terms": {"registar": 1, "registrar": 12}}), encoding="utf-8")
        self.assertIn("registrar", spelling.spelling_candidates("registar")["registar"])

    def test_candidates_never_modify_names_unicode_or_codes(self):
        for text in ("HNIT", "HUM-101", "honir\uff11", "certificaci\u00f3n",
                     "hnit\u2011101", "hnit\u0301"):
            self.assertEqual(spelling.spelling_candidates(text), {})
        hints = spelling.spelling_candidates(" ".join(["registar"] * 100))
        self.assertLessEqual(len(hints), 8)
        self.assertTrue(all(len(values) <= 8 for values in hints.values()))

    def test_model_output_is_not_learned_as_vocabulary(self):
        spelling.learn_terms(["microcredential"] * 3)
        before = self.path.read_bytes()
        parse_interpretation(response("microcredential policy"), "microcredentail policy")
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_or_invented_interpretations_are_rejected(self):
        invalid = [
            None, {}, [], ["text"], [{"type": "image"}], "not JSON", "[]", "{}",
            response("", query="policy"),
            response("policy", status="confident"),
            json.dumps({"status": "corrected", "question": 12, "search_query": "policy", "clarification": ""}),
            response("admission requirements"),
            response("latin honor requirements with a 1.25 GWA"),
            response("latin honor requirements", status="unchanged"),
            response("latin honor requirements", clarification="An answer"),
        ]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_interpretation(content, "latin hnit requirements")

    def test_numbers_codes_and_constraints_cannot_change(self):
        for original, altered in (
            ("GWA below 1.75", "GWA below 1.50"),
            ("GWA below 1.75", "GWA above 1.75"),
            ("BSIT requirements", "BSED requirements"),
            ("requirements for HUM-101", "requirements for HUM-102"),
            ("not required to enroll", "required to enroll"),
            ("I don't need a transfer", "I need a transfer"),
            ("before 2027 enrollment", "after 2027 enrollment"),
            ("is tuition free for BSIT", "is tuition fee for BSIT"),
            ("where is Dr. Jos\u00e9 Santos", "where is Dr. Juan Santos"),
            ("where is Dr. Maria Santos", "where is Dr. Maria Sanders"),
            ("BSIT REQUIREMENTS", "BSED REQUIREMENTS"),
            ("GWA <= 1.75", "GWA >= 1.75"),
            ("honir\u0301 policy", "honor policy"),
            ("enrollment on 2027/08/11", "enrollment on 2027/11/08"),
        ):
            with self.subTest(original=original), self.assertRaises(ValueError):
                parse_interpretation(response(altered), original)

    def test_unsafe_keyword_expansion_falls_back_to_valid_question(self):
        for original, query in (
            ("BSIT requirements with GWA below 1.75", "admission requirements"),
            ("latin honors requirements", "admission requirements"),
            ("is tuition free for BSIT", "tuition fees BSIT"),
            ("graduation requirements", "graduation requirements not eligible GWA below 2.5"),
        ):
            with self.subTest(original=original):
                parsed = parse_interpretation(response(original, status="unchanged", query=query), original)
                self.assertEqual(parsed.search_query, original)

    def test_follow_up_can_use_grounded_history_without_dropping_latest_subject(self):
        history = [{"role": "user", "content": "enrollment requirements"}]
        original = "and for transferees"
        query = "enrollment requirements for transferees"
        parsed = parse_interpretation(response(original, status="unchanged", query=query), original, history)
        self.assertEqual(parsed.search_query, query)
        latest = "latin honors requirements"
        parsed = parse_interpretation(response(latest, status="unchanged", query="enrollment requirements"), latest, history)
        self.assertEqual(parsed.search_query, latest)

    def test_clarification_does_not_choose_a_topic(self):
        original = "requirements for that thing"
        parsed = parse_interpretation(response(
            original, status="clarify", query="", clarification="Which policy do you mean?",
        ), original)
        self.assertEqual(parsed.question, original)
        self.assertEqual(parsed.search_query, "")
        self.assertIn("Ask ONLY one", interpretation_guidance(parsed))
        with self.assertRaises(ValueError):
            parse_interpretation(response("admission requirements", status="clarify", query="",
                                          clarification="Admission?"), original)

    def test_fences_and_text_blocks_are_supported(self):
        original = "enrolmnt requirements"
        content = response("enrollment requirements")
        self.assertEqual(parse_interpretation("```json\n" + content + "\n```", original).question,
                         "enrollment requirements")
        self.assertEqual(parse_interpretation([{"type": "text", "text": content}], original).question,
                         "enrollment requirements")

    def test_unavailable_interpretation_does_not_claim_success(self):
        self.assertIn("unavailable", interpretation_guidance(unavailable=True))
        self.assertIn("clarification", interpretation_guidance(unavailable=True))
        self.assertEqual(interpretation_guidance(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
