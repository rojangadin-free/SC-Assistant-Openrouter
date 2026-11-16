"""Offline contracts for general interpretation, not per-question replacement rules."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from threading import Event
from unittest.mock import patch

import _bootstrap  # noqa: F401

from rag import spelling
from rag.latency import should_optimize
from rag.query_optimizer import (
    OPTIMIZER_PROMPT,
    interpretation_guidance,
    optimizer_messages,
    parse_interpretation,
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
        spelling.reload_vocabulary()
        self.vocabulary.stop()
        self.temp.cleanup()

    def test_general_corrections_are_accepted_without_a_phrase_table(self):
        cases = [
            ("latin hnit requirements", "latin honor requirements"),
            ("latin hnir requirements", "latin honor requirements"),
            ("latn hnir", "latin honor"),
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

    def test_dynamodb_history_metadata_is_not_sent_to_interpretation(self):
        history = [
            {"role": "user", "content": "Which departments are listed?", "timestamp": Decimal(1794719000)},
            {"role": "assistant", "content": "See the current programs offered document.",
             "citations": [{"source": "Samar-College-2024.pdf", "pages": [Decimal(48)],
                            "score": Decimal("0.656")}],
             "private_metadata": {"identifier": "PRIVATE_METADATA_VALUE", "flags": {"one", "two"}}},
        ]
        before = deepcopy(history)
        payload = json.loads(optimizer_messages("college department", "college department", history)[1]["content"])
        self.assertEqual(payload["history"], [
            {"role": "user", "content": "Which departments are listed?"},
            {"role": "assistant", "content": "See the current programs offered document."},
        ])
        self.assertNotIn("PRIVATE_METADATA_VALUE", json.dumps(payload))
        self.assertEqual(history, before)

    def test_only_valid_dialogue_turns_are_used_for_interpretation(self):
        history = [
            {"role": "system", "content": "Previous subject: degree programs."},
            {"role": "tool", "content": "PRIVATE_TOOL_METADATA"},
            {"role": "assistant", "content": Decimal("1.25")},
            {"role": ["user"], "content": "INVALID_ROLE"},
            None,
            {"role": "user", "content": "college department"},
        ]
        payload = json.loads(optimizer_messages("college department", "college department", history)[1]["content"])
        self.assertEqual(payload["history"], [history[0], history[-1]])

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

    def test_missing_and_mistyped_vowels_are_repaired_without_a_model(self):
        for typed, meant in (
            ("latn hnir", "latin honor"),
            ("latin hnir requirements", "latin honor requirements"),
            ("what are latn hnir requirements?", "what are latin honor requirements?"),
            ("latn hnir for citas", "latin honor for citas"),
        ):
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

    def test_acceptance_uses_the_latest_assistant_offer_not_the_previous_cas_subject(self):
        offer = "share details on the currently active colleges instead"
        history = [{"role": "user", "content": "cas"}, {"role": "assistant", "content":
                   "OLD_CAS_CLAIM from the 2024 handbook. If you'd like, I can " + offer + "."}]
        before = deepcopy(history)
        for reply in ("yes do it", "yes", "YES", "yes, please", "okay", "sure", "go ahead", "please do that"):
            with self.subTest(reply=reply):
                parsed = parse_interpretation(response(reply, status="unchanged", query="CAS"), reply, history)
                self.assertEqual(parsed.question, reply)
                self.assertEqual(parsed.search_query, "the currently active colleges instead")
                self.assertEqual(parsed.resolved_request, offer)
                self.assertEqual(parsed.status, "unchanged")
                guidance = interpretation_guidance(parsed)
                self.assertIn("<accepted_offer>", guidance)
                self.assertIn(offer, guidance)
                self.assertNotIn("OLD_CAS_CLAIM", guidance)
        self.assertEqual(history, before)

    def test_explicit_informational_offers_are_general_not_a_college_phrase_table(self):
        for message, request, query in (
            ("Would you like me to explain the history of River University?",
             "explain the history of River University", "the history of River University"),
            ("I can list the requirements for HUM-101 in 2027.",
             "list the requirements for HUM-101 in 2027", "the requirements for HUM-101 in 2027"),
            ("I can list IT subjects for BSIT.", "list IT subjects for BSIT", "IT subjects for BSIT"),
            ("If you want, I could show you information about scholarships.",
             "show you information about scholarships", "scholarships"),
            ("Do you want me to compare the 2024 handbook and current offerings?",
             "compare the 2024 handbook and current offerings", "compare the 2024 handbook and current offerings"),
        ):
            with self.subTest(message=message):
                history = [{"role": "assistant", "content": message}]
                parsed = parse_interpretation(response("yes do it", status="unchanged"), "yes do it", history)
                self.assertEqual(parsed.resolved_request, request)
                self.assertEqual(parsed.search_query, query)

    def test_closing_invitation_takes_precedence_over_an_answered_opening_statement(self):
        history = [{"role": "user", "content": "cas"}, {"role": "assistant", "content":
                   "I can describe CAS from the older handbook. OLD_CAS_FACT is historical. "
                   "If you'd like, I can list the currently active colleges instead."}]
        parsed = parse_interpretation(response("yes do it", status="unchanged"), "yes do it", history)
        self.assertEqual(parsed.search_query, "the currently active colleges instead")
        self.assertNotIn("OLD_CAS_FACT", interpretation_guidance(parsed))

    def test_offer_resolution_does_not_swallow_rejections_new_topics_or_modifiers(self):
        from rag.query_optimizer import resolve_offer_acceptance

        history = [{"role": "assistant", "content": "I can list currently active colleges."}]
        for question in ("no", "no thanks", "not now", "citas", "history of samar college",
                         "yes but only BSIT", "yes do it for 2024", "do not do it", "thanks"):
            with self.subTest(question=question):
                self.assertIsNone(resolve_offer_acceptance(question, history))

    def test_acceptance_never_mines_an_old_offer_or_treats_a_quote_as_an_offer(self):
        from rag.query_optimizer import resolve_offer_acceptance

        histories = [
            [],
            [{"role": "assistant", "content": "The listed colleges include CITAS."}],
            [{"role": "assistant", "content": "I can list current colleges."},
             {"role": "user", "content": "Who is the registrar?"}],
            [{"role": "assistant", "content": "I can list current colleges."},
             {"role": "user", "content": "What is enrollment?"},
             {"role": "assistant", "content": "Enrollment is the registration process."}],
            [{"role": "assistant", "content": "> I can list current colleges."}],
            [{"role": "assistant", "content": "```text\nI can list current colleges.\n```"}],
            [{"role": "assistant", "content": "```text\nI can list current colleges."}],
            [{"role": "system", "content": "I can list current colleges."}],
            [{"role": "assistant", "content": "I can delete all your records."}],
        ]
        for history in histories:
            with self.subTest(history=history):
                self.assertIsNone(resolve_offer_acceptance("yes do it", history))

    def test_multiple_or_vague_offers_do_not_choose_a_topic_on_acceptance(self):
        for message in ("I can list current colleges or explain enrollment requirements.",
                        "I can list the colleges. I can explain the tuition fees.",
                        "If you'd like, I can share more details.",
                        "Would you like me to explain it?"):
            with self.subTest(message=message):
                parsed = parse_interpretation(response("yes do it", status="unchanged", query="enrollment"),
                                              "yes do it", [{"role": "assistant", "content": message}])
                self.assertEqual(parsed.status, "clarify")
                self.assertEqual(parsed.search_query, "")
                self.assertEqual(parsed.question, "yes do it")

    def test_explicit_unit_names_cannot_expand_from_assistant_history(self):
        for unit, replacement in (
            ("college of arts and science", "College of Liberal Arts and Sciences"),
            ("college of art and science", "College of Liberal Arts and Sciences"),
            ("College of Arts and Sciences", "College of Liberal Arts and Sciences"),
            ("cOlLeGe oF ArT aNd ScIeNcEs", "College of Liberal Arts and Sciences"),
            ("college of river studies", "College of Applied River Studies"),
            ("College of Valley Studies", "College of Valley Studies and Technology"),
        ):
            with self.subTest(unit=unit):
                original = f"Tell me about {unit}"
                history = [{"role": "user", "content": "Which college departments are listed?"},
                           {"role": "assistant", "content": f"The current catalog lists {replacement}."}]
                before = deepcopy(history)
                parsed = parse_interpretation(response(original, status="unchanged", query=replacement),
                                              original, history)
                self.assertEqual(parsed.question, original)
                self.assertEqual(parsed.search_query, original)
                self.assertEqual(history, before)

    def test_named_unit_substitutions_are_not_spelling_repairs(self):
        for original, altered in (("college of arts and science", "college of liberal arts and sciences"),
                                  ("college of river studies", "college of applied river studies"),
                                  ("CAS", "CLAS"), ("cas", "clas")):
            with self.subTest(original=original), self.assertRaises(ValueError):
                parse_interpretation(response(altered), original)

    def test_unit_acronym_queries_cannot_import_a_different_unit_from_history(self):
        for original, replacement in (("CAS", "College of Liberal Arts and Sciences (CLAS)"),
                                      ("cas", "College of Liberal Arts and Sciences (CLAS)"),
                                      ("CaS", "College of Liberal Arts and Sciences (CLAS)"),
                                      ("CRS", "College of Applied River Studies (CARS)")):
            with self.subTest(original=original):
                history = [{"role": "assistant", "content": f"The current catalog lists {replacement}."}]
                query = f"{original} {replacement}"
                parsed = parse_interpretation(response(original, status="unchanged", query=query), original, history)
                self.assertEqual(parsed.question, original)
                self.assertEqual(parsed.search_query, original)

    def test_known_acronyms_cannot_be_shortened_or_replaced_in_any_casing(self):
        history = [{"role": "user", "content": "cas"},
                   {"role": "assistant", "content": "College of Arts and Sciences (CAS)."}]
        for original, altered in (
            ("citas", "cas"), ("Citas", "cas"), ("CITAS", "CAS"),
            ("tell me about citas", "tell me about cas"),
            ("citas subjects", "cas subjects"),
            ("bsit tuition", "bsed tuition"),
        ):
            with self.subTest(original=original), self.assertRaises(ValueError):
                parse_interpretation(response(altered), original, history)
        parsed = parse_interpretation(response("CITAS"), "citas", history)
        self.assertEqual(parsed.question, "CITAS")

    def test_lowercase_citas_is_a_fresh_subject_not_a_cas_followup(self):
        history = [{"role": "user", "content": "cas"},
                   {"role": "assistant", "content": "CAS programs and departments."}]
        for query in ("cas", "citas cas programs"):
            parsed = parse_interpretation(response("citas", status="unchanged", query=query), "citas", history)
            self.assertEqual(parsed.question, "citas")
            self.assertEqual(parsed.search_query, "citas")
        payload = json.loads(optimizer_messages("citas", "citas", history)[1]["content"])
        self.assertEqual(payload["protected_acronyms"], ["citas"])
        self.assertEqual(payload["original_question"], "citas")

    def test_current_offerings_do_not_inherit_the_previous_cas_or_handbook_scope(self):
        original = "confirmed current offerings"
        history = [{"role": "user", "content": "cas"}, {"role": "assistant", "content":
                   "CAS reflects the 2024 handbook edition only, not confirmed current offerings."}]
        for query in (original + " CAS", original + " 2024 handbook", original):
            with self.subTest(query=query):
                parsed = parse_interpretation(response(original, status="unchanged", query=query), original, history)
                self.assertEqual(parsed.question, original)
                self.assertEqual(parsed.search_query, original)

    def test_confirmation_and_current_status_are_not_spelling_repairs(self):
        self.path.write_text(json.dumps({"terms": {"conferred": 40}}), encoding="utf-8")
        spelling.reload_vocabulary()
        self.assertEqual(spelling.correct_query("confirmed current offerings"), "")
        for original, altered in (("confirmed current offerings", "conferred current offerings"),
                                  ("current programs", "curated programs")):
            with self.subTest(original=original), self.assertRaises(ValueError):
                parse_interpretation(response(altered), original)
        parsed = parse_interpretation(response("current offerings"), "curent offerings")
        self.assertEqual(parsed.question, "current offerings")

    def test_institution_history_is_not_rewritten_as_the_previous_program_request(self):
        question = "history of samar college"
        history = [{"role": "user", "content": "confirmed current offerings"},
                   {"role": "assistant", "content": "CITAS and CBM curricula and organizational structure."}]
        for query in (question + " current programs CITAS", question + " curricula organizational structure"):
            with self.subTest(query=query):
                parsed = parse_interpretation(response(question, status="unchanged", query=query), question, history)
                self.assertEqual(parsed.question, question)
                self.assertEqual(parsed.search_query, question)

    def test_unnamed_followups_and_nonunit_codes_still_use_history(self):
        for original, topic, query in (
            ("what programs does it offer?", "College of Liberal Arts and Sciences",
             "what programs does College of Liberal Arts and Sciences offer?"),
            ("and for BSIT", "enrollment requirements", "enrollment requirements for BSIT"),
            ("and for HUM-101", "course prerequisites", "course prerequisites for HUM-101"),
            ("and for art", "course prerequisites", "course prerequisites for art"),
        ):
            with self.subTest(original=original):
                history = [{"role": "user", "content": topic}]
                parsed = parse_interpretation(response(original, status="unchanged", query=query), original, history)
                self.assertEqual(parsed.question, original)
                self.assertEqual(parsed.search_query, query)

    def test_general_department_list_does_not_inherit_previous_cas_subject(self):
        original = "college department"
        history = [{"role": "user", "content": "College of Arts and Sciences"},
                   {"role": "assistant", "content": "The handbook describes CAS."}]
        parsed = parse_interpretation(response(original, status="unchanged",
                                              query="College of Arts and Sciences college department"), original, history)
        self.assertEqual(parsed.search_query, original)

    def test_unit_reference_preserves_last_user_subject_not_wrong_assistant_name(self):
        original = "what programs does it offer?"
        history = [{"role": "user", "content": "College of Arts and Sciences"},
                   {"role": "assistant", "content": "College of Liberal Arts and Sciences offers Sociology."}]
        parsed = parse_interpretation(response(original, status="unchanged",
                                              query="College of Liberal Arts and Sciences programs offered"), original, history)
        self.assertEqual(parsed.search_query, "unit College of Arts and Sciences: what programs does it offer?")

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
