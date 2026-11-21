"""Offline contracts for shared semantic routing and authoritative user scope."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

from rag import source_policy as policy, spelling
from rag.query_intent import QueryIntent, decode_optimizer_output, resolve_query_intent


class QueryIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vocabulary = patch.object(spelling, "SPELL_VOCAB_FILE", str(Path(self.temp.name) / "vocabulary.json"))
        self.vocabulary.start()
        spelling.reload_vocabulary()

    def tearDown(self):
        spelling.reload_vocabulary()
        self.vocabulary.stop()
        self.temp.cleanup()

    def test_structured_output_separates_search_query_and_intent(self):
        output = json.dumps({"query": "graduate grading scale", "intent": "grading", "subject": ""})
        expected = ("graduate grading scale", QueryIntent("grading"))
        self.assertEqual(decode_optimizer_output(output, "fallback"), expected)
        self.assertEqual(decode_optimizer_output("```json\n" + output + "\n```", "fallback"), expected)

    def test_plain_text_or_invalid_metadata_cannot_supply_a_routing_intent(self):
        for value in ("BSIT tuition fees", "42", "None", "not JSON", ""):
            self.assertEqual(decode_optimizer_output(value, "fallback"), (value, None))
        for value in ('{"query": ""}', '{"intent": "grading"}', '{"query":', '```json\n{', '[{"query":"bad"}]'):
            self.assertEqual(decode_optimizer_output(value, "fallback"), ("fallback", None))
        for intent, subject in (("unknown", ""), (["grading"], ""), ("grading", 42), ("grading", "a" * 201)):
            query, metadata = decode_optimizer_output(json.dumps({"query": "usable", "intent": intent, "subject": subject}), "fallback")
            self.assertEqual(query, "usable")
            self.assertIsNone(metadata)

    def test_unfamiliar_paraphrases_reuse_existing_topic_handlers(self):
        cases = (
            ("How are term marks converted at graduate level?", QueryIntent("grading"), policy.grading_queries),
            ("Which classes would I study in IT?", QueryIntent("curriculum", "IT"), policy.curriculum_queries),
            ("recognition for academic achievers each semester", QueryIntent("deans_list"), policy.deans_list_queries),
            ("Explain how the institution grew over time", QueryIntent("history"), policy.institution_history_queries),
            ("What paperwork do incoming learners need to sign up?", QueryIntent("enrollment"), policy.enrollment_queries),
            ("Can two brothers get a reduction in what they pay?", QueryIntent("family_discount"), policy.family_discount_queries),
            ("What can I study at your school?", QueryIntent("program_offerings"), policy.program_catalog_queries),
            ("What academic divisions make up the school?", QueryIntent("departments"), policy.department_catalog_queries),
            ("Who runs the academic units?", QueryIntent("leadership"), policy.leadership_roster_queries),
        )
        for question, metadata, detector in cases:
            with self.subTest(question=question):
                self.assertFalse(detector(question))
                resolved = resolve_query_intent(question, metadata)
                self.assertNotEqual(resolved, question)
                self.assertTrue(detector(resolved), resolved)

    def test_known_requests_override_conflicting_model_classification(self):
        for question in ("grading undergraduate", "GS grading system", "IT subjects", "crim subjects",
                         "deans lister", "latin honors requirements", "history", "enrollment requirements",
                         "list all departments", "programs offered", "dean of College of Education", "BSIT"):
            with self.subTest(question=question):
                self.assertEqual(resolve_query_intent(question, QueryIntent("history")), question)
        self.assertTrue(policy.department_catalog_queries(resolve_query_intent("list all departments", QueryIntent("departments"))))

    def test_subjects_must_be_user_supplied_and_cannot_add_a_semester(self):
        question = "Which classes would I study in IT?"
        for metadata in (QueryIntent("curriculum", "BSCRIM"), QueryIntent("curriculum", "IT third year"), QueryIntent("curriculum")):
            self.assertEqual(resolve_query_intent(question, metadata), question)
        resolved = resolve_query_intent("Which classes would I study in IT 3rd year 1st sem 2024?", QueryIntent("curriculum", "IT"))
        for constraint in ("IT", "3rd year", "1st sem", "2024"):
            self.assertIn(constraint, resolved)
        self.assertNotIn("BSCRIM", resolved)

    def test_assistant_claims_are_not_a_source_for_subject_identity(self):
        question = "what about that one?"
        self.assertEqual(resolve_query_intent(question, QueryIntent("curriculum", "IT"),
                                            [{"role": "assistant", "content": "IT"}]), question)
        resolved = resolve_query_intent(question, QueryIntent("curriculum", "IT"),
                                        [{"role": "user", "content": "IT"}])
        self.assertTrue(policy.curriculum_queries(resolved))

    def test_complete_query_cannot_obtain_a_subject_from_an_unrelated_user_turn(self):
        question = "Which classes would I study in IT?"
        history = [{"role": "user", "content": "BSCRIM subjects"}]
        self.assertEqual(resolve_query_intent(question, QueryIntent("curriculum", "BSCRIM"), history), question)

    def test_private_excluded_compound_and_single_topic_requests_are_not_reclassified(self):
        for question in ("what are my grades?", "my marks", "how much are my fees?", "my subjects", "my enrollment history", "not about grading",
                         "fees excluding BSIT", "grading and tuition", "tuition; admissions", "library"):
            self.assertEqual(resolve_query_intent(question, QueryIntent("grading")), question)
        for question in (None, 42, "", "   "):
            self.assertEqual(resolve_query_intent(question, QueryIntent("grading")), question)

    def test_grading_followup_still_works_without_metadata_or_despite_wrong_metadata(self):
        history = [{"role": "user", "content": "grading system"}]
        self.assertEqual(resolve_query_intent("graduate level", None, history), "graduate grading system")
        self.assertEqual(resolve_query_intent("graduate level", QueryIntent("program_offerings"), history), "graduate grading system")

    def test_all_followup_expands_the_previous_enrollment_request(self):
        history = [{"role": "user", "content": "enrollment requirements"},
                   {"role": "assistant", "content": "Only the new-student requirements."}]
        resolved = resolve_query_intent("ALL", None, history)
        self.assertIn("all enrollment requirements", resolved)
        self.assertIn("new students", resolved)
        self.assertIn("transferees", resolved)
        self.assertIn("old students", resolved)
        self.assertIn("final step", resolved)
        self.assertNotIn("Only the new-student requirements", resolved)
        self.assertEqual(resolve_query_intent("ALL", None, []), "ALL")

    def test_all_followup_preserves_the_previous_non_enrollment_topic(self):
        for previous, marker in (("IT subjects", "all years and semesters"),
                                 ("grading system", "complete grading scale"),
                                 ("deans lister", "complete eligibility"),
                                 ("history", "complete details")):
            with self.subTest(previous=previous):
                self.assertIn(marker, resolve_query_intent("all of them", None,
                                                           [{"role": "user", "content": previous}]))


if __name__ == "__main__":
    unittest.main()
