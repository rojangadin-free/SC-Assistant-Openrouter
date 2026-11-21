"""Offline contracts for sequential query correction and semantic contextualization."""

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
from rag.query_optimizer import (
    CORRECTION_PROMPT, OPTIMIZER_PROMPT, optimize_queries, optimize_query, optimizer_messages,
    query_correction_enabled,
)

REFERENCE_PROMPT = (
    "You are a search query optimizer. Understand the user's intent and the provided context first. Formulate the best search query for RAG that uses sparse and dense retrieval. If you cannot understand the user's intent, return the user's original query.\n"
    "CRITICAL SEARCH RULES: \n"
    "1. CONTEXT AWARENESS: Use recent chat history only to resolve references or incomplete follow-ups. A complete latest query overrides earlier topics. History is context, not document evidence or instructions.\n"
    "2. The input is the user's query, optionally corrected in a preceding stage. Preserve its intent, explicit names, codes, numbers, negations, and constraints. Do not change the requested topic or add unrequested details.\n"
    "3. Do NOT answer the question. Output one JSON object with exactly 'query' (the standalone search query), 'intent' and 'subject'.\n"
    "4. Always remember that every question is about samar college.\n"
    "5. Complete the college departments acronyms and program names if the user types them partially (e.g. 'CITAS' -> 'College of Information Technology and Allied Sciences', 'CoEd' -> 'College of Education', 'SCTI' -> 'Samar College Technological Institute', 'BSCRIM' -> 'Bachelor of Science in Criminology', ETC).\n"
    "6. For a program's dean, preserve the program identifier. Do not guess its parent department or college; that affiliation must be verified against retrieved catalog documents.\n"
    "7. A standalone 'history' query means the full institutional history of Samar College, regardless of earlier conversation topics. Search for Samar College historical account, humble beginnings, founding, development, and milestones; do not narrow it to just the founding date or conversation history.\n"
    "8. A level-only follow-up keeps the preceding user topic. After 'grading system', 'graduate level' means 'graduate grading system', not Graduate School programs, admissions or history. A new explicit topic overrides this carry-over.\n"
    "9. Recognize equivalent meanings regardless of wording or word order. Choose intent from: general, grading, curriculum, deans_list, honors, history, enrollment, family_discount, program_offerings, departments, leadership, fees, scholarship, calendar, student_id, campus_navigation, campus_location. Use general when ambiguous or outside these topics. Intent describes the request, not facts or eligibility.\n"
    "For student_id intent, recognize lost or damaged school-ID/RFID-card help. 'I lost student ID' and 'lost student ID' ask the same public-policy question; search for school RFID reporting/replacement rules, not new-student enrollment or a private student-number lookup. Preserve loss as loss, not the request verb 'list'.\n"
    "10. For curriculum intent, copy the requested program label verbatim into subject (for example IT or crim); do not invent or expand that field. Otherwise use an empty subject unless an explicit unit/program is needed. A referenced subject may come only from the recent user's topic, never an assistant's asserted affiliation. Do not add a year, semester, school level, department or program that the user did not request.\n"
    "11. If the latest query is only 'ALL', 'everything', or 'all of them', use recent user history to expand the immediately preceding topic to all of its applicable categories. After 'enrollment requirements', 'ALL' means every enrollment procedure category and final step, not a generic college overview.\n"
    "12. Output a query best for BM25 sparse retrieval and dense embedding search. Avoid stopwords, filler words, and conversational phrases. Use only the essential keywords that capture the user's intent and subject. Do not include any instructions, clarifications, or additional context in the query.\n"
    "13. Always consider the question is in waray waray languange, so be careful on the correction. eg. 'dean hit cas' -> 'dean of cas' and 'dean hit coed' -> 'dean of coed' and 'dean hit scti' -> 'dean of scti' and 'dean hit clas' -> 'dean of clas' and 'dean hit gs' -> 'dean of gs' and 'dean hit cocje' -> 'dean of cocje'.\n"
    "15. ENGLISH RETRIEVAL QUERY: Translate Waray-Waray, Tagalog and mixed-language input into an English search query in the JSON 'query' field, then contextualize it using only eligible history. Documents are English. Preserve the original meaning, who/what is affected, hypothetical wording, negations, dates and explicit names/program codes; do not invent a lost object or a policy. Keep the 'subject' field copied verbatim as required above. Remove laughter/courtesy from search keywords when it adds no meaning. This English query is for retrieval only: it must not change the user's original language or the answer language. Do not output an answer or a translation preface.\n"
    "16. Waray 'diin it SC?' and 'diin mabibilngan it SC' both mean 'Where is Samar College located?'. Here 'it' is a Waray article, not an English pronoun; the named school makes the question complete. Use campus_location and English address/location search terms. Do not import an ID, fees, or a prior subject, and do not fabricate the address.\n"
)


class QueryOptimizerTests(unittest.TestCase):
    def test_named_waray_location_question_does_not_send_history_or_accept_card_invention(self):
        for question in ("diin it sc?", "diin mabibilngan it sc"):
            with self.subTest(question=question):
                model = Mock()
                model.invoke.side_effect = [SimpleNamespace(content="I lost my student ID at SC"),
                                            SimpleNamespace(content="Samar College address location")]
                corrected, search = optimize_queries(model, question, [
                    {"role": "user", "content": "I lost my student ID"},
                    {"role": "assistant", "content": "OLD_ID_HISTORY"},
                ])
                self.assertEqual(corrected, question)
                self.assertEqual(search, "Samar College address location")
                self.assertIn("No previous history.", model.invoke.call_args.args[0][1]["content"])
                for call in model.invoke.call_args_list:
                    self.assertNotIn("OLD_ID_HISTORY", str(call.args[0]))

    def test_waray_search_translation_keeps_original_and_uses_no_extra_model_call(self):
        cases = (
            ("Pila an bayad han tuition?", "Samar College tuition fees", "fees"),
            ("nawawara an ak student ID ha SC", "Samar College lost RFID identification card reporting policy", "student_id"),
            ("magaanu man ak kun nawawara ak ha SC HAHAHAHA", "What should I do if I get lost at Samar College campus?", "campus_navigation"),
            ("Ano an grading han undergraduate?", "undergraduate college grading system numerical percentage scale", "grading"),
        )
        for original, english, intent in cases:
            for enabled in ("true", "false"):
                with self.subTest(original=original, correction=enabled), patch.dict(os.environ, {
                    "QUERY_CORRECTION_ENABLED": enabled,
                }):
                    model = Mock()
                    contextualized = SimpleNamespace(content=json.dumps({"query": english, "intent": intent, "subject": ""}))
                    model.invoke.side_effect = ([SimpleNamespace(content=original), contextualized]
                                                if enabled == "true" else [contextualized])
                    self.assertEqual(optimize_queries(model, original), (original, english))
                    self.assertEqual(model.invoke.call_count, 2 if enabled == "true" else 1)
                    self.assertIn("Latest Question: " + original, model.invoke.call_args.args[0][1]["content"])
                    self.assertIn("ENGLISH RETRIEVAL QUERY", model.invoke.call_args.args[0][0]["content"])

    def test_waray_correction_and_context_cannot_invent_an_id_from_a_lost_speaker(self):
        question = "magaanu man ak kun nawawara ak ha SC HAHAHAHA"
        wrong = "I lost my student ID at Samar College"
        model = Mock()
        model.invoke.side_effect = [SimpleNamespace(content=wrong), SimpleNamespace(content=json.dumps({
            "query": "student ID replacement policy", "intent": "student_id", "subject": "",
        }))]
        intents = []
        history = [{"role": "user", "content": "I lost my student ID"}]
        self.assertEqual(optimize_queries(model, question, history, on_intent=intents.append), (question, question))
        self.assertEqual(intents, [])
        self.assertIn("Latest Question: " + question, model.invoke.call_args_list[1].args[0][1]["content"])
        self.assertIn("No previous history.", model.invoke.call_args_list[1].args[0][1]["content"])

    def test_unrecognized_non_id_subject_also_cannot_gain_a_card_in_correction(self):
        question = "Nawawara ak yana HAHAHA"
        model = Mock()
        model.invoke.side_effect = [SimpleNamespace(content="I lost my student ID"),
                                    SimpleNamespace(content="student ID RFID replacement")]
        self.assertEqual(optimize_queries(model, question), (question, question))

    def setUp(self):
        self.correction_flag = patch.dict(os.environ, {"QUERY_CORRECTION_ENABLED": "true"})
        self.correction_flag.start()
        self.addCleanup(self.correction_flag.stop)
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

    def test_correction_toggle_defaults_on_and_accepts_explicit_boolean_values(self):
        with patch.dict(os.environ):
            os.environ.pop("QUERY_CORRECTION_ENABLED", None)
            self.assertTrue(query_correction_enabled())
        for value in ("true", "1", "yes", "on", " TRUE "):
            with patch.dict(os.environ, {"QUERY_CORRECTION_ENABLED": value}):
                self.assertTrue(query_correction_enabled())
        for value in ("false", "0", "no", "off", " FALSE "):
            with patch.dict(os.environ, {"QUERY_CORRECTION_ENABLED": value}):
                self.assertFalse(query_correction_enabled())

    def test_disabled_correction_still_contextualizes_and_emits_semantic_intent(self):
        from rag.query_intent import QueryIntent

        question = "what arre the requiremnts for enrollemtn"
        model = Mock()
        model.invoke.return_value = SimpleNamespace(content=json.dumps({
            "query": "enrollment requirements", "intent": "enrollment", "subject": "",
        }))
        intents = []
        with patch.dict(os.environ, {"QUERY_CORRECTION_ENABLED": "false"}), \
                patch("rag.query_optimizer.correct_query") as local, \
                patch("rag.query_optimizer.misspellings") as hints:
            result = optimize_queries(model, question, [{"role": "user", "content": "grading system"}],
                                      on_intent=intents.append)
        self.assertEqual(result, (question, "enrollment requirements"))
        model.invoke.assert_called_once()
        self.assertEqual(model.invoke.call_args.args[0][0], {"role": "system", "content": OPTIMIZER_PROMPT})
        self.assertIn("Latest Question: " + question, model.invoke.call_args.args[0][1]["content"])
        self.assertNotIn("LIKELY MISSPELLINGS", model.invoke.call_args.args[0][1]["content"])
        self.assertEqual(intents, [QueryIntent("enrollment")])
        local.assert_not_called()
        hints.assert_not_called()

    def test_disabled_correction_preserves_followup_history_and_failure_fallback(self):
        history = [{"role": "user", "content": "grading system"}]
        with patch.dict(os.environ, {"QUERY_CORRECTION_ENABLED": "false"}):
            model = Mock()
            model.invoke.return_value = SimpleNamespace(content="graduate grading system scale")
            self.assertEqual(optimize_queries(model, "graduate level", history),
                             ("graduate level", "graduate grading system scale"))
            model.invoke.assert_called_once()
            self.assertIn("User: grading system", model.invoke.call_args.args[0][1]["content"])
            self.assertIn("Latest Question: graduate grading system", model.invoke.call_args.args[0][1]["content"])
            model.invoke.side_effect = RuntimeError("offline")
            self.assertEqual(optimize_queries(model, "graduate level", history),
                             ("graduate level", "graduate grading system"))

    def test_disabled_correction_empty_query_does_not_call_model(self):
        model = Mock()
        with patch.dict(os.environ, {"QUERY_CORRECTION_ENABLED": "false"}):
            self.assertEqual(optimize_queries(model, " \n"), ("", ""))
        model.invoke.assert_not_called()

    def test_empty_history_uses_the_reference_placeholder(self):
        for history in (None, []):
            with self.subTest(history=history):
                self.assertEqual(optimizer_messages("Who is the registrar?", history)[1], {
                    "role": "user",
                    "content": "Chat History:\nNo previous history.\n\nLatest Question: Who is the registrar?",
                })

    def test_complete_queries_do_not_send_history_in_either_model_call(self):
        history = [{"role": "user", "content": "OLD_GRADUATE_TOPIC"},
                   {"role": "assistant", "content": "OLD_GRADUATE_ANSWER"}]
        before = deepcopy(history)
        for question in ("Who is the dean of College of Education?", "What are the enrollment requirements?",
                         "IT subjects", "Which classes would I study in IT?", "crim subjects", "deans lister",
                         "grading undergraduate", "history", "college department", "library",
                         "latin honir requirements", "What if I lost my ID?", "What are the fees for this semester?",
                         "Are there scholarships for transferees?", "What about BSIT subjects?"):
            with self.subTest(question=question):
                model = Mock()
                model.invoke.side_effect = [SimpleNamespace(content=question), SimpleNamespace(content=question)]
                optimize_queries(model, question, history)
                self.assertEqual(model.invoke.call_count, 2)
                for call in model.invoke.call_args_list:
                    for message in call.args[0]:
                        self.assertNotIn("OLD_GRADUATE_TOPIC", message["content"])
                        self.assertNotIn("OLD_GRADUATE_ANSWER", message["content"])
                self.assertIn("No previous history.", model.invoke.call_args_list[1].args[0][1]["content"])
        self.assertEqual(history, before)

    def test_incomplete_queries_keep_history_only_in_contextualization(self):
        history = [{"role": "user", "content": "grading system"},
                   {"role": "assistant", "content": "PREVIOUS_TOPIC_ANSWER"}]
        for question in ("graduate level", "what are its requirements?", "and for BSIT", "what about that one?",
                         "How much?", "HOW MUCH IS IT?", "3", "yes do it", "what about for transferees?"):
            with self.subTest(question=question):
                model = Mock()
                model.invoke.side_effect = [SimpleNamespace(content=question), SimpleNamespace(content=question)]
                optimize_queries(model, question, history)
                first, second = model.invoke.call_args_list
                self.assertEqual(first.args[0], [{"role": "system", "content": CORRECTION_PROMPT},
                                                {"role": "user", "content": question}])
                self.assertIn("User: grading system", second.args[0][1]["content"])
                self.assertIn("Assistant: PREVIOUS_TOPIC_ANSWER", second.args[0][1]["content"])

    def test_direct_optimizer_calls_also_exclude_history_for_complete_queries(self):
        history = [{"role": "user", "content": "OLD_TOPIC_SHOULD_NOT_BE_SENT"}]
        model = Mock(return_value=None)
        model.invoke.return_value = SimpleNamespace(content="college grading system")
        optimize_query(model, "grading undergraduate", history)
        self.assertNotIn("OLD_TOPIC_SHOULD_NOT_BE_SENT", model.invoke.call_args.args[0][1]["content"])

    def test_model_correction_cannot_enable_history_for_a_complete_original_query(self):
        model = Mock()
        model.invoke.side_effect = [SimpleNamespace(content="graduate level"), SimpleNamespace(content="graduate level")]
        history = [{"role": "user", "content": "PRIVATE_PREVIOUS_TOPIC"}]
        optimize_queries(model, "grading undergraduate", history)
        for call in model.invoke.call_args_list:
            self.assertNotIn("PRIVATE_PREVIOUS_TOPIC", str(call.args[0]))

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
        body = optimizer_messages("what about that department?", history)[1]["content"]
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
        body = optimizer_messages("what about that department?", history)[1]["content"]
        self.assertEqual(body, "Chat History:\nSystem: Previous subject: degree programs.\n"
                               "User: college department\n\nLatest Question: what about that department?")

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

    def test_correction_precedes_contextualization(self):
        history = [{"role": "user", "content": "SCTI programs"}]
        model = Mock()
        model.invoke.side_effect = [
            SimpleNamespace(content="What are its requirements?"),
            SimpleNamespace(content="SCTI program requirements Samar College"),
        ]
        with patch("rag.query_optimizer.correct_query", return_value=""):
            result = optimize_queries(model, "what is its requirments?", history)
        self.assertEqual(result, ("What are its requirements?", "SCTI program requirements Samar College"))
        first, second = model.invoke.call_args_list
        self.assertEqual(first.args[0], [
            {"role": "system", "content": CORRECTION_PROMPT},
            {"role": "user", "content": "what is its requirments?"},
        ])
        self.assertEqual(second.args[0], optimizer_messages(result[0], history))

    def test_correction_cannot_replace_an_explicit_topic_with_an_unrelated_one(self):
        for question, wrong in (("i lost student ID", "new student enrollment requirements"),
                                ("grading undergraduate", "programs offered"),
                                ("deans lister", "dean of College of Education")):
            with self.subTest(question=question):
                model = Mock()
                model.invoke.side_effect = [SimpleNamespace(content=wrong), SimpleNamespace(content=question)]
                corrected, _ = optimize_queries(model, question)
                self.assertEqual(corrected, question)
                self.assertEqual(model.invoke.call_args_list[1].args[0], optimizer_messages(question))

    def test_same_topic_lost_id_correction_is_still_allowed(self):
        model = Mock()
        model.invoke.side_effect = [SimpleNamespace(content="I lost my student ID."),
                                    SimpleNamespace(content="school RFID lost card policy")]
        self.assertEqual(optimize_queries(model, "i lost student ID"),
                         ("I lost my student ID.", "school RFID lost card policy"))

    def test_level_followup_is_resolved_only_after_correction_before_contextualization(self):
        history = [{"role": "user", "content": "grading system"},
                   {"role": "assistant", "content": "COLLEGE_ANSWER_NOT_EVIDENCE"}]
        before = deepcopy(history)
        model = Mock()
        model.invoke.side_effect = [SimpleNamespace(content="graduate level."),
                                    SimpleNamespace(content="Graduate School grading system numerical scale")]
        self.assertEqual(optimize_queries(model, "graduate level.", history),
                         ("graduate level.", "Graduate School grading system numerical scale"))
        self.assertEqual(model.invoke.call_args_list[0].args[0], [
            {"role": "system", "content": CORRECTION_PROMPT}, {"role": "user", "content": "graduate level."},
        ])
        self.assertEqual(model.invoke.call_args_list[1].args[0],
                         optimizer_messages("graduate grading system", history, history_question="graduate level."))
        self.assertEqual(history, before)

    def test_grading_followup_context_survives_model_failure(self):
        model = Mock()
        model.invoke.side_effect = RuntimeError("offline")
        history = [{"role": "user", "content": "grading system"}]
        self.assertEqual(optimize_queries(model, "graduate level", history),
                         ("graduate level", "graduate grading system"))

    def test_semantic_metadata_uses_the_existing_call_and_keeps_tuple_api(self):
        from rag.query_intent import QueryIntent

        question = "How are term marks converted at graduate level?"
        model = Mock()
        model.invoke.side_effect = [SimpleNamespace(content=question), SimpleNamespace(content=json.dumps({
            "query": "Graduate School grading scale", "intent": "grading", "subject": "",
        }))]
        intents = []
        self.assertEqual(optimize_queries(model, question, on_intent=intents.append),
                         (question, "Graduate School grading scale"))
        self.assertEqual(intents, [QueryIntent("grading")])
        self.assertEqual(model.invoke.call_count, 2)

    def test_contextualization_failure_keeps_corrected_query(self):
        model = Mock()
        model.invoke.side_effect = [SimpleNamespace(content="Who is the BSIT dean?"), RuntimeError("offline")]
        self.assertEqual(optimize_queries(model, "who are BSIT dean?"),
                         ("Who is the BSIT dean?", "Who is the BSIT dean?"))

    def test_correction_failure_still_contextualizes_local_repair(self):
        model = Mock()
        model.invoke.side_effect = [RuntimeError("offline"), SimpleNamespace(content="Samar College enrollment requirements")]
        with patch("rag.query_optimizer.correct_query", return_value="enrollment requirements"):
            result = optimize_queries(model, "enrolmnet requirments")
        self.assertEqual(result, ("enrollment requirements", "Samar College enrollment requirements"))
        self.assertIn("Latest Question: enrollment requirements", model.invoke.call_args.args[0][1]["content"])

    def test_invalid_or_empty_correction_preserves_query(self):
        for content in (None, 42, "", " \n", [], [{"text": None}]):
            with self.subTest(content=content), patch("rag.query_optimizer.correct_query", return_value=""):
                model = Mock()
                model.invoke.side_effect = [SimpleNamespace(content=content), SimpleNamespace(content="")]
                self.assertEqual(optimize_queries(model, "not BSIT"), ("not BSIT", "not BSIT"))

    def test_empty_query_does_not_call_model(self):
        model = Mock()
        self.assertEqual(optimize_queries(model, " \n"), ("", ""))
        model.invoke.assert_not_called()

    def test_dean_of_educ_is_locally_corrected_even_when_models_fail(self):
        model = Mock()
        model.invoke.side_effect = RuntimeError("offline")
        self.assertEqual(optimize_queries(model, "dean of educ"),
                         ("dean of education", "dean of education"))
        self.assertEqual(model.invoke.call_args_list[0].args[0][1]["content"], "dean of education")
        self.assertIn("Latest Question: dean of education", model.invoke.call_args_list[1].args[0][1]["content"])

    def test_crim_reaches_both_query_stages_without_becoming_crime(self):
        vocabulary = spelling._Vocabulary({**spelling._vocabulary().freq, "crime": 1000})
        with patch.object(spelling, "_vocabulary", return_value=vocabulary):
            for question in ("crim", "crim subjects", "dean of crim"):
                with self.subTest(question=question):
                    model = Mock()
                    model.invoke.side_effect = RuntimeError("offline")
                    self.assertEqual(optimize_queries(model, question), (question, question))
                    self.assertEqual(model.invoke.call_args_list[0].args[0][1]["content"], question)
                    self.assertEqual(model.invoke.call_args_list[1].args[0], optimizer_messages(question))
                    self.assertNotIn("LIKELY MISSPELLINGS", model.invoke.call_args_list[1].args[0][1]["content"])

    def test_educ_repair_is_contextual_and_preserves_course_identifiers(self):
        for typed, expected in (("dean of educ", "dean of education"),
                                ("who is the dean of Educ?", "who is the dean of education?"),
                                ("dean of the educ,", "dean of the education,")):
            with self.subTest(typed=typed):
                self.assertEqual(spelling.correct_query(typed), expected)
        for question in ("EDUC-101 requirements", "dean of EDUC-101", "dean of educ101",
                         "dean of EDUC", "educ course requirements", "BS EDUC requirements"):
            with self.subTest(question=question):
                self.assertEqual(spelling.correct_query(question), "")

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
