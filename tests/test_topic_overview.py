"""Offline standalone-topic retrieval and answer-scope regressions."""
import json
import os
import unittest
from unittest.mock import Mock, patch

import test_newer_retrieval as fixtures


class TopicOverviewTests(unittest.TestCase):
    def test_equivalent_waray_campus_location_queries_find_address_without_old_topic_or_document_absence_claims(self):
        h = self.harness
        letterhead = fixtures.document("SCTI_Program.docx", "SAMAR COLLEGES, INC.\nMabini Avenue, Catbalogan City Samar",
                                       "street-address", section_heading="SCTI PROGRAM OFFERED")
        city = fixtures.document("Library_Services_2026.docx", "Samar Colleges, Inc. | Catbalogan City, Samar",
                                 "city-address", section_heading="Footer & Contact Information")
        office = fixtures.document("Office_Directory_2026.docx", "OSAS: Ground Floor, New Building, Samar Colleges, Inc., Catbalogan City.",
                                   "office", section_heading="Office Locations")
        old_site = fixtures.document(fixtures.BASE, "In 1949 Samar College had a main building at San Roque Street and Allen Avenue.",
                                     "old-site", section_heading="The Humble Beginnings of Samar College")
        english = "Samar College official physical address location"
        for question in ("diin it sc?", "diin mabibilngan it sc"):
            for correction in ("true", "false"):
                for rewrite in (None, english, "College of Education office location", json.dumps({"query": "student ID replacement", "intent": "student_id", "subject": ""})):
                    with self.subTest(question=question, correction=correction, rewrite=rewrite), patch.dict(os.environ, {
                        "QUERY_CORRECTION_ENABLED": correction, "RAG_SOURCE_FILTER_ENABLED": "true",
                    }):
                        hybrid = h.install([office, letterhead, city], baseline=[old_site])
                        h.ns["query_optimizer_model"].invoke = Mock(
                            side_effect=RuntimeError("offline") if rewrite is None else None,
                            return_value=fixtures.SimpleNamespace(content=rewrite),
                        )
                        result = h.chat(question, history=[{"role": "user", "content": "I lost my student ID"},
                                                           {"role": "assistant", "content": "OLD_CARD_TOPIC"}])
                        self.assertEqual(set(result["citations"]), {"street-address", "city-address"}, h.output.getvalue())
                        prompt = result["messages_to_llm"][0].content
                        self.assertIn(letterhead.page_content, prompt)
                        self.assertIn(city.page_content, prompt)
                        self.assertIn("<campus_location_request>", prompt)
                        self.assertIn("Never say there is no document", prompt)
                        self.assertIn("a documented city/province answers", prompt)
                        self.assertIn("offering unrelated programs/admission/help", prompt)
                        for unrelated in (office.page_content, old_site.page_content, "OLD_CARD_TOPIC", "<student_id_request>", "<standalone_topic_overview>"):
                            self.assertNotIn(unrelated, prompt)
                        self.assertEqual(result["messages_to_llm"][-1].content, question)
                        self.assertTrue(any("institutional letterhead" in query for _, query, _ in hybrid.calls))
                        self.assertFalse(any("list of fees and amounts" in query for _, query, _ in hybrid.calls))
                        if rewrite is not None:
                            self.assertNotIn("OLD_CARD_TOPIC", str(h.ns["query_optimizer_model"].invoke.call_args.args[0]))

    def test_unsupported_location_is_fact_uncertainty_without_fake_coverage_or_external_referral(self):
        h = self.harness
        office = fixtures.document("Directory.docx", "OSAS: Ground Floor, New Building.", "office", section_heading="Office Locations")
        h.install([office], baseline=[])
        h.ns["query_optimizer_model"].invoke.side_effect = RuntimeError("offline")
        result = h.chat("diin it sc?")
        self.assertEqual(result["citations"], [])
        prompt = result["messages_to_llm"][0].content
        self.assertNotIn(office.page_content, prompt)
        self.assertIn("<campus_location_request>", prompt)
        self.assertIn("say only that you cannot confirm that fact", prompt)
        self.assertIn("without inventing an address, referring to external pages/contact channels", prompt)

    def test_english_search_translation_does_not_change_the_waray_reply_language_source(self):
        h = self.harness
        language_namespace = {}
        fixtures.definitions("src/prompt.py", {"response_language_prompt"}, language_namespace)
        language_policy = language_namespace["response_language_prompt"]
        loss = fixtures.document(fixtures.BASE, "Lost RFID must be reported to Security Management Office.",
                                 "rfid", section_heading="Radio Frequency Identification (RFID)")
        original = "nawawara an ak student ID ha SC"
        english = "Samar College lost RFID identification card reporting policy"
        for enabled in ("true", "false"):
            # Include a provider that wrongly translates during correction: the
            # original language must still determine the visible answer language.
            for corrected in (None, "I lost my student ID at Samar College."):
                with self.subTest(correction=enabled, corrected=corrected), patch.dict(os.environ, {
                    "QUERY_CORRECTION_ENABLED": enabled,
                }), patch.dict(h.ns, {"response_language_prompt": language_policy}):
                    hybrid = h.install([], baseline=[loss])
                    h.ns["query_optimizer_model"].invoke = Mock(return_value=fixtures.SimpleNamespace(content=json.dumps({
                        "query": english, "intent": "student_id", "subject": "",
                    })))
                    result = h.chat(original, corrected=corrected)
                    self.assertEqual(result["citations"], ["rfid"], h.output.getvalue())
                    prompt = result["messages_to_llm"][0].content
                    self.assertTrue(prompt.endswith(language_policy))
                    self.assertIn("Waray-Waray input: answer in Waray-Waray", prompt)
                    self.assertIn("A Waray query translated to English for search still receives a Waray answer", prompt)
                    if corrected and enabled == "true":
                        self.assertIn("<reply_language_source>", prompt)
                        self.assertIn(repr(original), prompt)
                        self.assertEqual(result["messages_to_llm"][-1].content, corrected)
                    else:
                        self.assertEqual(result["messages_to_llm"][-1].content, original)
                    self.assertTrue(any(query == english for _, query, _ in hybrid.calls))
                    self.assertIn(loss.page_content, prompt)

    def test_waray_lost_speaker_cannot_turn_into_id_loss_even_after_an_id_conversation(self):
        h = self.harness
        question = "magaanu man ak kun nawawara ak ha SC HAHAHAHA"
        id_policy = fixtures.document(fixtures.BASE, "Lost RFID must be reported to Security Management Office.",
                                      "rfid", section_heading="Radio Frequency Identification (RFID)")
        office = fixtures.document("Office_Directory_2026.docx", "Office of Student Affairs and Services (OSAS), Ground Floor, New Building.",
                                   "office-location", section_heading="Office Locations")
        wrong = json.dumps({"query": "student ID replacement", "intent": "student_id", "subject": ""})
        for correction in ("true", "false"):
            for first_stage in (None, "I lost my student ID at SC"):
                with self.subTest(correction=correction, first_stage=first_stage), patch.dict(os.environ, {
                    "QUERY_CORRECTION_ENABLED": correction, "RAG_SOURCE_FILTER_ENABLED": "true",
                }), patch.dict(h.ns, {"get_asker_role": lambda state: "guest"}):
                    hybrid = h.install([office], baseline=[id_policy])
                    h.ns["query_optimizer_model"].invoke = Mock(return_value=fixtures.SimpleNamespace(content=wrong))
                    result = h.chat(question, corrected=first_stage, history=[
                        {"role": "user", "content": "I lost my student ID"},
                        {"role": "assistant", "content": "OLD_ID_RESPONSE"},
                    ])
                    self.assertEqual(result["citations"], [], h.output.getvalue())
                    self.assertEqual(result["messages_to_llm"][-1].content, question)
                    prompt = result["messages_to_llm"][0].content
                    self.assertIn("<campus_navigation_request>", prompt)
                    self.assertIn("ask briefly where", prompt)
                    self.assertNotIn("<student_id_request>", prompt)
                    self.assertNotIn("<standalone_topic_overview>", prompt)
                    for unrelated in (id_policy.page_content, office.page_content, "OLD_ID_RESPONSE"):
                        self.assertNotIn(unrelated, prompt)
                    self.assertFalse(any("student ID" in query or "RFID" in query for _, query, _ in hybrid.calls))
                    self.assertTrue(any("campus map" in query for _, query, _ in hybrid.calls))

    def test_first_person_and_fragment_lost_id_select_the_same_rfid_policy(self):
        h = self.harness
        # Actual handbook wording, deliberately without literal 'student ID'.
        loss = fixtures.document(fixtures.BASE, "Lost RFID must be reported to Security Management Office.",
                                 "rfid-loss", page=133, section_heading="MISTS Services Offered to Students / Radio Frequency Identification (RFID)")
        prices = fixtures.document(fixtures.BASE, "Price of school RFID is Php 280.00 pesos complete set, if\n"
                                   "RFID only Php 200.00 pesos and lace only 80.00 pesos.", "rfid-prices", page=133,
                                   section_heading="MISTS Services Offered to Students / Radio Frequency Identification (RFID)")
        enrollment = fixtures.document("REGISTRAR_Services_&_Information_2026.docx", "New Students and Transferees\n"
                                       "Secure ID Information Form. Present Plotting Form for Student Number.", "id-enrollment",
                                       section_heading="Enrollment Procedure / New Students and Transferees")
        lab = fixtures.document(fixtures.BASE, "A lost or damaged computer unit requires a depreciated replacement charge.",
                                "lab-equipment", section_heading="Computer Laboratory Rules")
        variants = ("i lost student ID", "lost student ID", "I lost my student ID.",
                    "my student ID is lost", "I misplaced my school card", "I lost my RFID",
                    "nawawara an ak student ID ha SC", "nawara an ak RFID")
        for question in variants:
            for enabled in ("true", "false"):
                for rewrite in (None, "new student enrollment requirements ID form", json.dumps({
                    "query": "new student enrollment requirements ID form", "intent": "enrollment", "subject": "",
                })):
                    with self.subTest(question=question, correction=enabled, rewrite=rewrite), patch.dict(os.environ, {
                        "QUERY_CORRECTION_ENABLED": enabled, "RAG_SOURCE_FILTER_ENABLED": "true",
                    }), patch.dict(h.ns, {"FINAL_TOP_K": 1, "get_asker_role": lambda state: "guest"}):
                        hybrid = h.install([enrollment], baseline=[lab, loss, prices])
                        h.ns["query_optimizer_model"].invoke = Mock(
                            side_effect=RuntimeError("offline") if rewrite is None else None,
                            return_value=fixtures.SimpleNamespace(content=rewrite),
                        )
                        result = h.chat(question, role="guest", history=[
                            {"role": "user", "content": "enrollment requirements"},
                            {"role": "assistant", "content": "OLD_ENROLLMENT_ANSWER"},
                        ])
                        self.assertEqual(set(result["citations"]), {"rfid-loss", "rfid-prices"}, h.output.getvalue())
                        prompt = result["messages_to_llm"][0].content
                        self.assertIn(loss.page_content, prompt)
                        self.assertIn(prices.page_content, prompt)
                        self.assertIn("<student_id_request>", prompt)
                        self.assertNotIn("<standalone_topic_overview>", prompt)
                        self.assertNotIn("<enrollment_request>", prompt)
                        for wrong in (enrollment.page_content, lab.page_content, "OLD_ENROLLMENT_ANSWER"):
                            self.assertNotIn(wrong, prompt)
                        self.assertEqual(result["messages_to_llm"][-1].content, question)
                        self.assertTrue(any("RFID" in query for _, query, _ in hybrid.calls))
                        if rewrite is not None:
                            sent = h.ns["query_optimizer_model"].invoke.call_args.args[0]
                            self.assertNotIn("OLD_ENROLLMENT_ANSWER", str(sent))

    def test_correction_off_skips_repairs_but_still_contextualizes_and_retrieves(self):
        h = self.harness
        question = "what arre the requiremnts for enrollemtn"
        policy = fixtures.document("Enrollment_Guide_2026.docx", "Enrollment requires a completed application form.", "enrollment")
        local = Mock(side_effect=AssertionError("local correction must be disabled"))
        model = Mock(return_value=fixtures.SimpleNamespace(content=json.dumps({
            "query": "enrollment requirements", "intent": "enrollment", "subject": "",
        })))
        with patch.dict(os.environ, {"QUERY_CORRECTION_ENABLED": "false"}), \
                patch.dict(h.ns, {"correct_query": local}):
            hybrid = h.install([policy], baseline=[])
            h.ns["query_optimizer_model"].invoke = model
            result = h.chat(question)
        local.assert_not_called()
        model.assert_called_once()
        self.assertEqual(model.call_args.args[0][0]["content"], h.optimizer.OPTIMIZER_PROMPT)
        self.assertIn("Latest Question: " + question, model.call_args.args[0][1]["content"])
        self.assertFalse(h.retrieval.call_args.kwargs["allow_spelling"])
        self.assertEqual(h.retrieval.call_args.kwargs["original_question"], question)
        self.assertEqual(result["messages_to_llm"][-1].content, question)
        self.assertEqual(result["citations"], ["enrollment"], h.output.getvalue())
        self.assertTrue(any(query == "enrollment requirements" for _, query, _ in hybrid.calls))

    def test_semantic_intent_is_shared_by_retrieval_evidence_and_answer_scope(self):
        h = self.harness
        grade = fixtures.document(fixtures.BASE, "1.0 | 100 | Excellent\n1.8 | 85 | Passing\n1.9-5.0 | 84-below | Failure",
                                  "gs-scale", section_heading="GRADING SYSTEM OF THE GS")
        rule = fixtures.document(fixtures.BASE, "The Graduate School uses the Averaging System with Midterm and Final periods.\n"
                                 "A student must not have a grade lower than 1.7 to continue.",
                                 "gs-rule", section_heading="GRADING SYSTEM OF THE GS")
        courses = [fixtures.document(fixtures.BASE, "COURSE CODE | COURSE TITLE | UNITS\n"
                                     f"IT {101 + index} | Subject {index} | 3", f"it-{index}",
                                     section_heading="Bachelor of Science in Information Technology / First Year / "
                                     + semester + " Semester") for index, semester in enumerate(("First", "Second"))]
        recognition = fixtures.document(fixtures.BASE, "Dean's List academic recognition is awarded each semester.",
                                        "recognition", section_heading="Academic Recognition")
        beginnings = fixtures.document(fixtures.BASE, "The college opened its doors in the documented founding year.",
                                       "beginnings", section_heading="Historical Account")
        growth = fixtures.document(fixtures.BASE, "It subsequently expanded its buildings over the following decades.",
                                   "growth", section_heading="Historical Account")
        noise = fixtures.document(fixtures.BASE, "Graduate School admission requires a bachelor's degree.",
                                  "admission", section_heading="Graduate School / Admission")
        cases = (
            ("How are term marks converted at graduate level?", "grading", "", "college grading scale", {"gs-scale", "gs-rule"}, "<grading_system_request>"),
            ("Which classes would I study in IT?", "curriculum", "IT", "IT curriculum course subjects", {"it-0", "it-1"}, "<curriculum_subject_request>"),
            ("recognition for academic achievers each semester", "deans_list", "", "Dean's List recognition", {"recognition"}, "<deans_list_academic_request>"),
            ("What does making the academic roll take?", "deans_list", "", "Dean's List eligibility requirements", {"recognition"}, "<deans_list_academic_request>"),
            ("Explain how the institution grew over time", "history", "", "Samar College historical account", {"beginnings", "growth"}, "<institution_history_request>"),
        )
        for question, intent, subject, search_query, expected, tag in cases:
            with self.subTest(intent=intent), patch.dict(h.ns, {"FINAL_TOP_K": 2 if intent == "history" else 1}):
                hybrid = h.install([], baseline=[noise, grade, rule, *courses, recognition, beginnings, growth])
                h.ns["query_optimizer_model"].invoke = Mock(return_value=fixtures.SimpleNamespace(content=json.dumps({
                    "query": search_query, "intent": intent, "subject": subject,
                })))
                result = h.chat(question)
                self.assertEqual(set(result["citations"]), expected, h.output.getvalue())
                prompt = result["messages_to_llm"][0].content
                self.assertIn(tag, prompt)
                self.assertIn("<resolved_followup>", prompt)
                self.assertNotIn("<standalone_topic_overview>", prompt)
                self.assertNotIn(noise.page_content, prompt)
                if question == "What does making the academic roll take?":
                    self.assertNotIn("<deans_list_overview>", prompt)
                self.assertEqual(h.retrieval.call_args.kwargs["original_question"], question)
                self.assertNotEqual(h.retrieval.call_args.kwargs["user_question"], question)
                self.assertEqual(result["messages_to_llm"][-1].content, question)
                probes = {query for _, query, _ in hybrid.calls}
                self.assertIn(question, probes)
                self.assertIn(search_query, probes)

    def setUp(self):
        self.harness = fixtures.NewerRetrievalTests(methodName="runTest")
        self.harness.assert_offline = self.assert_offline
        self.addCleanup(self.harness.doCleanups)
        self.harness.setUp()

    def assert_offline(self):
        self.assertEqual(self.harness.denied_imports, [])
        self.assertEqual(self.harness.env_reads, [])
        for connection in self.harness.sockets:
            connection.assert_not_called()

    def test_standalone_topics_do_not_capture_scoped_or_referential_questions(self):
        from rag.latency import overview_topic

        for question, topic in (("classrooms", "classrooms"), ("classroom", "classroom"),
                                ("classrooms pls?", "classrooms"), ("tell me about classrooms", "classrooms"),
                                ("laboratories", "laboratories"), ("library services", "library services")):
            self.assertEqual(overview_topic(question), topic)
        for question in ("", "how many classrooms?", "current classrooms", "classroom locations",
                         "classroom schedule", "classroom assignments", "what about classrooms?", "my classroom", "where is it?",
                         "classrooms and laboratories", "programs", "deans list", "dean of BSCRIM",
                         "BSCRIM", "CoEd", "SCTI", "enrollment", "thanks", "hello", "classroom 204"):
            with self.subTest(question=question):
                self.assertEqual(overview_topic(question), "")

    def test_topic_expansion_is_neutral_not_a_complete_inventory(self):
        queries = self.harness.ns["expand_queries"]("classrooms", allow_spelling=False)
        self.assertEqual(queries[0], "classrooms")
        self.assertIn("classrooms overview description", queries)
        self.assertNotIn("complete list of classrooms", queries)
        self.assertNotIn("classrooms include the following", queries)

    def test_body_evidence_allows_plural_forms_but_not_unrelated_source_titles(self):
        h = self.harness
        rules = fixtures.document(fixtures.BASE, "Students shall keep the classroom and campus clean and free from litter.", "rules")
        facilities = fixtures.document(fixtures.BASE, "The Founders Building was constructed with five modern-styled classrooms.", "facilities")
        unrelated = fixtures.document("Classrooms_2026.docx", "College programs include business management and teacher education.", "unrelated")
        contents = fixtures.document(fixtures.BASE, "Table of contents\nClassrooms .......... 17", "contents")
        reference = fixtures.document(fixtures.BASE, "See the handbook for information about classrooms.", "reference")
        heading_only = fixtures.document("Campus.docx", "[SECTION: CLASSROOMS]\nAdmission requires a valid application.", "heading-only")
        concise = fixtures.document("Facilities_2026.docx", "Classrooms: 12", "concise")
        self.assertEqual(h.policy.overview_documents([unrelated, contents, heading_only, rules, facilities, reference, concise], "classrooms"),
                         [rules, facilities, concise])
        self.assertEqual(h.policy.overview_documents([rules, facilities], "classroom"), [rules, facilities])

    def test_classroom_context_survives_optimizer_failures_source_modes_and_context_cap(self):
        h = self.harness
        facilities = fixtures.document(fixtures.BASE, "The Founders Building was constructed with five modern-styled classrooms.", "facilities", page=17)
        rules = fixtures.document(fixtures.BASE, "Students shall keep the classroom and campus clean and free from litter.", "rules", page=89)
        unrelated = fixtures.document("Programs_2026.docx", "College programs include business management and teacher education.", "unrelated")
        fixtures.definitions("src/prompt.py", {"system_prompt"}, h.ns)
        for question in ("classrooms", "classroom", "classrooms pls?"):
            for mode in ("true", "false"):
                for rewrite in ("classroom current room assignments and schedule", None):
                    with self.subTest(question=question, mode=mode, rewrite=rewrite), patch.dict(
                        os.environ, {"RAG_SOURCE_FILTER_ENABLED": mode}
                    ), patch.dict(h.ns, {"FINAL_TOP_K": 2}):
                        h.install([unrelated], baseline=[facilities, rules])
                        h.ns["query_optimizer_model"].invoke = Mock(
                            side_effect=RuntimeError("optimizer unavailable") if rewrite is None else None,
                            return_value=fixtures.SimpleNamespace(content=rewrite),
                        )
                        result = h.chat(question, history=[{"role": "assistant", "content": "Classroom information is unavailable."}])
                        self.assertEqual(set(result["citations"]), {"facilities", "rules"}, h.output.getvalue())
                        prompt = result["messages_to_llm"][0].content
                        self.assertIn("<standalone_topic_overview>", prompt)
                        self.assertIn("Do not ask for clarification merely", prompt)
                        self.assertIn("Preserve historical or proposed wording", prompt)
                        self.assertIn("No concluding note", prompt)
                        self.assertNotIn("Classroom information is unavailable.", prompt)
                        self.assertNotIn("business management", prompt)
                        self.assertEqual(result["messages_to_llm"][-1].content, question)

    def test_grading_system_keeps_scale_computation_and_rules_outside_overview_limits(self):
        h = self.harness
        policy = [
            fixtures.document(fixtures.BASE, "Grading System\nThe final grade uses class standing and examination performance.",
                              "grading-intro", page=40, section_heading="Grading System"),
            fixtures.document(fixtures.BASE, "Numerical Rating | Percentage Equivalent | Description\n"
                              "1.00 | 98-100 | Excellent\n1.25 | 95-97 | Very Good",
                              "scale-top", page=41, section_heading="Grading System / Numerical Ratings"),
            fixtures.document(fixtures.BASE, "Numerical Rating | Percentage Equivalent | Description\n"
                              "3.00 | 75 | Passing\n5.00 | Below 75 | Failing\nINC | Incomplete | Completion required",
                              "scale-tail", page=42, section_heading="Grading System / Numerical Ratings"),
            fixtures.document(fixtures.BASE, "Component | Weight\nClass standing | 40%\nExamination | 60%",
                              "computation", page=43, section_heading="Computation of Grades"),
            fixtures.document(fixtures.BASE, "An incomplete grade requires completion of the missing work within the stated period.",
                              "special-marks", page=44, section_heading="Incomplete Grades"),
        ]
        other = fixtures.document("Elementary_2026.docx", "Grading System\n"
                                  "Written work 30%, performance tasks 50%, quarterly assessment 20%.",
                                  "elementary", section_heading="Elementary / Grading System")
        course = fixtures.document("Curriculum_2026.docx", "Grading System course\nIT 101 | Computer Programming | 3",
                                   "curriculum", section_heading="Curriculum Subjects")
        for mode in ("true", "false"):
            for rewrite in (None, "elementary grading system performance tasks"):
                with self.subTest(mode=mode, rewrite=rewrite), patch.dict(os.environ, {
                    "RAG_SOURCE_FILTER_ENABLED": mode,
                }), patch.dict(h.ns, {"FINAL_TOP_K": 2}):
                    hybrid = h.install([other, course], baseline=policy)

                    def filtered(query, metadata_filter):
                        if metadata_filter == {"source": {"$eq": fixtures.BASE}}:
                            if "computation" in query.lower():
                                return policy[3:4]
                            if "incomplete" in query.lower():
                                return policy[4:]
                            return policy[1:3]
                        return hybrid.matching(query, metadata_filter)

                    hybrid.filtered = filtered
                    hybrid.ensemble.invoke.side_effect = lambda query: [policy[0], other, course]
                    h.ns["query_optimizer_model"].invoke = Mock(
                        side_effect=RuntimeError("offline") if rewrite is None else None,
                        return_value=fixtures.SimpleNamespace(content=rewrite),
                    )
                    result = h.chat("grading system", history=[{"role": "assistant", "content": "STALE_GRADE_RULES"}])
                    self.assertEqual(set(result["citations"]), {doc.metadata["chunk_id"] for doc in policy}, h.output.getvalue())
                    prompt = result["messages_to_llm"][0].content
                    for doc in policy:
                        self.assertIn(doc.page_content, prompt)
                    self.assertIn("<grading_system_request>", prompt)
                    self.assertNotIn("<standalone_topic_overview>", prompt)
                    for wrong in (other.page_content, course.page_content, "STALE_GRADE_RULES"):
                        self.assertNotIn(wrong, prompt)
                    self.assertEqual(result["messages_to_llm"][-1].content, "grading system")

    def test_current_grading_update_is_not_completed_with_an_old_scale(self):
        h = self.harness
        current = fixtures.document("Grading_Policy_2026.docx", "Grading System\n"
                                    "The final grade uses examinations 70% and class standing 30%.", "current",
                                    section_heading="College / Grading System")
        old = fixtures.document(fixtures.BASE, "Numerical Rating | Percentage Equivalent\n"
                                "1.00 | OLD_SCALE_RANGE", "old-scale", section_heading="Grading System")
        graduate = fixtures.document("Graduate_2026.docx", "Grading System\n"
                                     "Grade 1.0 indicates GRADUATE_ONLY_RULE.", "graduate",
                                     section_heading="Graduate School / Grading System")
        for mode in ("true", "false"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"RAG_SOURCE_FILTER_ENABLED": mode}):
                h.install([current, graduate], baseline=[old])
                result = h.chat("grading system")
                self.assertEqual(result["citations"], ["current"])
                prompt = result["messages_to_llm"][0].content
                self.assertIn("<source_selection>", prompt)
                self.assertNotIn("OLD_SCALE_RANGE", prompt)
                self.assertNotIn("GRADUATE_ONLY_RULE", prompt)

    def test_graduate_level_follow_up_keeps_grading_topic_instead_of_school_overview(self):
        h = self.harness
        college = fixtures.document(fixtures.BASE, "Grading System\n1.0 | 98-100\n3.0 | 75\n"
                                    "Passed, Failed, Dropped", "college-scale", page=43, section_heading="GRADING SYSTEM")
        graduate = [
            fixtures.document(fixtures.BASE, "Percentage Equivalent | Numerical Rating | Adjectival Rating\n"
                              "100 | 1.0 | Excellent\n99-98 | 1.1 | Outstanding\n97-96 | 1.2 | Outstanding\n"
                              "95-94 | 1.3 | Very Good\n93-92 | 1.4 | Very Good\n91-90 | 1.5 | Satisfactory\n"
                              "89-88 | 1.6 | Satisfactory\n87-86 | 1.7 | Fair\n85 | 1.8 | Passing\n84-below | 1.9-5.0 | Failure",
                              "gs-scale", page=18, section_heading="GRADING SYSTEM OF THE GS"),
            fixtures.document(fixtures.BASE, "The Graduate School uses the Averaging System with Midterm and Final Examination periods.\n"
                              "A student must not have a grade lower than 1.7 to continue in the master's degree.",
                              "gs-rules", page=18, section_heading="GRADING SYSTEM OF THE GS"),
        ]
        overview = fixtures.document(fixtures.BASE, "The Graduate School offers Master of Arts in Education.\n"
                                     "Admission: applicants need a bachelor's degree.\nRecognition was granted in 1996.",
                                     "gs-overview", page=16, section_heading="The Graduate School / Admission")
        for mode in ("true", "false"):
            for rewrite in (None, "Graduate School programs admission history", "graduate grading system"):
                with self.subTest(mode=mode, rewrite=rewrite), patch.dict(os.environ, {
                    "RAG_SOURCE_FILTER_ENABLED": mode,
                }), patch.dict(h.ns, {"FINAL_TOP_K": 1}):
                    h.install([], baseline=[overview, college, *graduate])
                    h.ns["query_optimizer_model"].invoke = Mock(
                        side_effect=RuntimeError("offline") if rewrite is None else None,
                        return_value=fixtures.SimpleNamespace(content=rewrite),
                    )
                    first = h.chat("grading system")
                    self.assertEqual(first["citations"], ["college-scale"])
                    history = [{"role": "user", "content": "grading system"},
                               {"role": "assistant", "content": "COLLEGE_RESPONSE_NOT_GRADUATE_EVIDENCE"}]
                    result = h.chat("graduate level.", history=history)
                    self.assertEqual(set(result["citations"]), {"gs-scale", "gs-rules"}, h.output.getvalue())
                    prompt = result["messages_to_llm"][0].content
                    for passage in graduate:
                        self.assertIn(passage.page_content, prompt)
                    self.assertIn("<grading_system_request>", prompt)
                    self.assertIn("graduate grading system", prompt)
                    self.assertNotIn("<standalone_topic_overview>", prompt)
                    for unrelated in (overview.page_content, college.page_content, "COLLEGE_RESPONSE_NOT_GRADUATE_EVIDENCE"):
                        self.assertNotIn(unrelated, prompt)
                    self.assertEqual(h.retrieval.call_args.kwargs["user_question"], "graduate grading system")
                    self.assertEqual(result["messages_to_llm"][-1].content, "graduate level.")
                    if rewrite is not None:
                        self.assertIn("Latest Question: graduate grading system",
                                      h.ns["query_optimizer_model"].invoke.call_args.args[0][1]["content"])

    def test_grading_followup_is_resolved_when_optimizer_pool_is_busy(self):
        h = self.harness
        college = fixtures.document(fixtures.BASE, "Grading System\n1.0 | 98-100\n3.0 | 75", "college",
                                    section_heading="Grading System")
        graduate = fixtures.document(fixtures.BASE, "The Graduate School grading system uses the Averaging System.\n"
                                     "Grade 1.8 | 85 | Passing", "graduate", section_heading="GRADING SYSTEM OF THE GS")
        h.install([], baseline=[college, graduate])
        pool = Mock()
        pool.submit.side_effect = h.ns["CapacityError"]("busy")
        with patch.dict(h.ns, {"optimizer_pool": pool}):
            result = h.chat("graduate level", history=[{"role": "user", "content": "grading system"},
                                                      {"role": "assistant", "content": "Previous college answer."}])
        self.assertEqual(result["citations"], ["graduate"])
        self.assertIn("<resolved_followup>", result["messages_to_llm"][0].content)
        self.assertEqual(result["messages_to_llm"][-1].content, "graduate level")
        h.ns["query_optimizer_model"].invoke.assert_not_called()

    def test_equivalent_undergraduate_wording_selects_the_same_complete_grading_evidence(self):
        h = self.harness
        college = [
            fixtures.document(fixtures.BASE, "Numerical Rating | Percentage Equivalent\n"
                              "1.0 | 98-100\n1.1 | 96-97\n2.0 | 85", "college-top", page=43,
                              section_heading="GRADING SYSTEM"),
            fixtures.document(fixtures.BASE, "Numerical Rating | Percentage Equivalent\n"
                              "2.1 | 84\n3.0 | 75\n3.1-5.0 | 74 and below (Failed/Dropped/Not Taken)\n"
                              "Final rating remarks shall be Passed, Failed or Dropped.", "college-tail", page=43,
                              section_heading="GRADING SYSTEM"),
        ]
        graduate = fixtures.document(fixtures.BASE, "The Graduate School uses the Averaging System.\n"
                                     "1.0 | 100 | Excellent\n1.9-5.0 | 84 and below | Failure", "gs",
                                     section_heading="GRADING SYSTEM OF THE GS")
        for question in ("grading system", "grading undergraduate", "undergraduate grading", "undergrad grading",
                         "grading for undergraduates", "grading at the undergraduate level", "college grading",
                         "undergraduate system of grading", "grading"):
            for rewrite in (None, "graduate grading system", "undergraduate admissions programs"):
                with self.subTest(question=question, rewrite=rewrite), patch.dict(h.ns, {"FINAL_TOP_K": 1}):
                    h.install([], baseline=[graduate, *college])
                    h.ns["query_optimizer_model"].invoke = Mock(
                        side_effect=RuntimeError("offline") if rewrite is None else None,
                        return_value=fixtures.SimpleNamespace(content=rewrite),
                    )
                    history = [{"role": "user", "content": "graduate grading system"},
                               {"role": "assistant", "content": "PREVIOUS_GRADUATE_RULES"}]
                    result = h.chat(question, history=history)
                    self.assertEqual(set(result["citations"]), {"college-top", "college-tail"}, h.output.getvalue())
                    prompt = result["messages_to_llm"][0].content
                    for doc in college:
                        self.assertIn(doc.page_content, prompt)
                    self.assertIn("<grading_system_request>", prompt)
                    self.assertNotIn("<standalone_topic_overview>", prompt)
                    self.assertNotIn("<program_list_request>", prompt)
                    self.assertNotIn(graduate.page_content, prompt)
                    self.assertNotIn("PREVIOUS_GRADUATE_RULES", prompt)
                    self.assertEqual(h.retrieval.call_args.kwargs["user_question"], question)
                    self.assertEqual(result["messages_to_llm"][-1].content, question)


if __name__ == "__main__":
    unittest.main()
