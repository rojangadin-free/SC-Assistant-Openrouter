"""Offline contracts for shared semantic routing and authoritative user scope."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401

from rag import source_policy as policy, spelling
from rag.query_intent import QueryIntent, decode_optimizer_output, known_query_intents, query_needs_history, query_rewrite_supported, resolve_query_intent


class QueryIntentTests(unittest.TestCase):
    def test_payment_terms_detector_recognizes_each_arrangement_phrase(self):
        for question in (
            "does samar college allow partial payment?", "partial payment", "partial payments",
            "installment", "installments", "instalment", "instalments", "can I pay in installments?",
            "payment plan", "payment scheme", "payment terms", "payment plans", "payment schemes",
            "down payment", "downpayment", "down-payment", "Can I pay in parts?",
            "Can I pay in two payments?", "Can I pay in three payments?", "Can I pay in two or three payments?",
            "Can I pay in 2 payments?", "Can I pay in 3 separate payments?", "Can I pay later?", "pay later",
            "hulugan", "Pwede ba hulugan ang tuition?",
        ):
            with self.subTest(question=question):
                self.assertTrue(policy.payment_terms_queries(question))
                self.assertIn("payment_terms", known_query_intents(question))
                self.assertIn("payment_terms", policy.matching_policy_intents(question))

    def test_payment_terms_detector_excludes_prices_aid_refunds_and_named_study_loans(self):
        for question in (
            "how much is the tuition", "sibling discount", "family discount", "scholarship requirements",
            "full or partial scholarship", "athletes' full or partial tuition fees", "Family discount Full Payment",
            "refund of entrance fee", "what is Study Now Pay Later?", "Study-Now-Pay-Later Plan (SNPLP)",
            "SNPLP requirements", "how do I enroll", "when do classes start", "",
        ):
            with self.subTest(question=question):
                self.assertFalse(policy.payment_terms_queries(question))
                self.assertNotIn("payment_terms", known_query_intents(question))
                self.assertEqual(resolve_query_intent(question, QueryIntent("payment_terms")), question)
        for question in (None, 42):
            self.assertFalse(policy.payment_terms_queries(question))

    def test_payment_questions_keep_partial_payment_for_every_conflicting_label(self):
        question = "does samar college allow partial payment?"
        for label in ("fees", "scholarship", "financial_aid", "enrollment"):
            with self.subTest(label=label):
                resolved = resolve_query_intent(question, QueryIntent(label))
                self.assertEqual(resolved, question)
                self.assertIn("partial payment", resolved)

    def test_labels_supplement_uncovered_words_even_without_a_deterministic_intent(self):
        question = "does samar college allow partial payment?"
        # Exercise the structural fallback, not merely the new detector's early return.
        with patch("rag.query_intent.known_query_intents", return_value=set()):
            for label in ("fees", "scholarship", "financial_aid", "enrollment"):
                with self.subTest(label=label):
                    resolved = resolve_query_intent(question, QueryIntent(label))
                    self.assertTrue(resolved.startswith(question + " "), resolved)
                    self.assertIn("partial payment", resolved)
                    self.assertIn("undergraduate", resolved)
        question = "Are staggered tuition contributions possible?"
        self.assertFalse(known_query_intents(question))
        resolved = resolve_query_intent(question, QueryIntent("fees"))
        self.assertTrue(resolved.startswith(question + " "), resolved)
        self.assertIn("tuition fees", resolved)

    def test_covered_short_fee_topics_keep_the_existing_exact_rewrite(self):
        self.assertEqual(resolve_query_intent("tuition fees", QueryIntent("fees")), "tuition fees")
        self.assertEqual(resolve_query_intent("college tuition fees", QueryIntent("fees")), "undergraduate tuition fees")

    def test_supplements_do_not_duplicate_a_named_unit_identity(self):
        from rag.unit_identity import requested_unit

        for question in ("How much is tuition for the Graduate School?",
                         "How much is tuition for the College of Education?"):
            with self.subTest(question=question):
                unit = requested_unit(question)
                self.assertIsNotNone(unit)
                resolved = resolve_query_intent(question, QueryIntent("fees"))
                self.assertTrue(resolved.startswith(question + " "), resolved)
                self.assertIsNotNone(requested_unit(resolved), resolved)
                self.assertEqual(requested_unit(resolved).name, unit.name, resolved)

    def test_payment_rewrites_cannot_replace_arrangements_with_aid_or_enrollment(self):
        for question in ("does samar college allow partial payment?", "can I pay in installments?", "hulugan"):
            for rewritten in ("undergraduate tuition fees", "scholarship eligibility requirements",
                              "available tuition discounts scholarships grants financial aid", "enrollment procedure registration documents steps"):
                with self.subTest(question=question, rewritten=rewritten):
                    self.assertFalse(query_rewrite_supported(question, rewritten, preserve_topic=False))
        self.assertTrue(query_rewrite_supported("Pwede ba hulugan ang tuition?", "tuition installment payment terms", preserve_topic=False))

    def test_payment_guidance_requires_uncertainty_and_scoped_contact_referrals(self):
        guidance = " ".join(policy.PAYMENT_TERMS_GUIDANCE.split())
        for required in (
            "Use only supplied evidence", "explicitly states a school payment plan",
            "Scholarships", "tuition discounts", "Full Payment table header", "government study loans",
            "Study-Now-Pay-Later / SNPLP", "refund/cancellation rules are NOT payment arrangements",
            "Do not infer permission OR prohibition", "I can't confirm that from the available documents",
            "documents do not state whether partial payment or installments are allowed",
            "Do not answer yes or no", "Cashier/Finance", "Tuition and Other Fees Focal Person",
            "Samar_Colleges_OSAS_Extracted-v2_2026.docx", "then STOP",
            "Do not state or imply the amount, schedule or percentage of any payment",
            "refund timing rule, quote the supplied rule accurately", "never call it an installment rule",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guidance)

    def test_payment_evidence_separates_policy_contacts_and_requested_refund_liability(self):
        def passage(text, heading="", source="Samar-College-2024.pdf"):
            return SimpleNamespace(page_content=text, metadata={"source": source, "section_heading": heading})
        lookalikes = [
            passage("Government Study Loan Program: Study-Now-Pay-Later Plan (SNPLP).", "Government Funded Scholarship Grants"),
            passage("Entrance scholarship is good for one semester; full or partial scholarship requires academic scholarship eligibility.", "Entrance Scholarship"),
            passage("Athletes may enjoy full or partial tuition fees and tuition fee discounts.", "Sports Scholarship"),
            passage("Number of Family Members | Full Payment | 20% | 15% | 10%\n2 | | | |", "Members of the Family Nucleus"),
            passage("Tuition fee schedule: amounts by unit.", "Tuition Fees", "Synthetic_Fee_Schedule_2026.docx"),
            passage("The school has not specified whether tuition installment payment plans are allowed.", "Fees"),
        ]
        refund = passage("Upon cancellation of registration, the student shall pay the entire tuition and other school fees "
                         "for the whole term on the third week after the start of regular classes;", "Refund and Other Charges")
        contact = passage("Tuition and Other Fees Focal Person\nJORLIE N. LAPARA, CPA", "Institutional Student Programs and Services",
                          "Samar_Colleges_OSAS_Extracted-v2_2026.docx")
        pool = [*lookalikes, refund, contact]
        self.assertEqual(policy.payment_terms_documents(pool), [contact])
        self.assertEqual(policy.payment_terms_documents(pool, policy_only=True), [])
        payment = policy.matching_policy_intents("can I pay in installments?")["payment_terms"]
        self.assertEqual(payment.documents(pool), [contact])
        self.assertEqual(payment.coverage(pool), [])
        compound = policy.matching_policy_intents("Can I pay in installments, and what is the refund timing rule?")["payment_terms"]
        self.assertEqual(compound.documents(pool), [refund, contact])
        for statement in ("Students may pay tuition in installments.", "Tuition installment payments are not permitted."):
            explicit = passage(statement, "School Fees Payment Terms", "Synthetic_Payment_Policy.docx")
            self.assertEqual(policy.payment_terms_documents([explicit], policy_only=True), [explicit])

    def test_payment_referrals_and_questions_do_not_establish_a_policy(self):
        for text in (
            "For questions about tuition installment payment terms, you may contact the Cashier.",
            "Ask Finance whether partial payment is permitted.",
            "Can students pay tuition in installments? Contact Finance for confirmation.",
            "Payment plans for tuition have not been announced; students must contact Finance.",
        ):
            with self.subTest(text=text):
                document = SimpleNamespace(page_content=text, metadata={"source": "Synthetic_Fees_Referral_2026.docx"})
                self.assertEqual(policy.payment_terms_documents([document], policy_only=True), [])

    def test_requested_refund_context_respects_exclusions_not_policy_negation(self):
        refund = SimpleNamespace(page_content="The student shall pay the entire tuition and other school fees for the whole term "
                                 "on the third week after the start of regular classes;", metadata={"source": "Samar-College-2024.pdf"})
        for question in (
            "Can I pay in installments? I am not asking about refunds.",
            "Can I pay in installments without cancellation or refunds?",
            "Can I pay in installments, excluding refunds?",
            "Can I pay in installments? Ignore cancellation rules.",
        ):
            with self.subTest(question=question):
                self.assertEqual(policy.matching_policy_intents(question)["payment_terms"].documents([refund]), [])
        question = "Can I pay in installments and why is there no refund after two weeks?"
        self.assertEqual(policy.matching_policy_intents(question)["payment_terms"].documents([refund]), [refund])

    def test_received_dropped_subject_status_keeps_its_public_policy_intent(self):
        variants = (
            "what do i do if i get drop on a subject?",
            "What should I do if I get dropped in a subject?",
            "I was dropped from a class", "My course was dropped",
            "My instructor dropped me", "The teacher dropped my subject",
            "I was dropped by my professor", "My grade is Dropped (DR)",
            "What does a DR remark mean?", "Dropped subject",
            "na-drop ako", "nadrop ako sa subject", "na drop ako sa subject ko",
            "na-drop ak", "gin-drop ak ha subject", "ma-drop ako sa subject",
            "na-drop ang subject ko", "gin-drop an akon subject",
            "what do i do if i got dropped?", "What do I do if I get dropped?", "i got dropped",
            "what happens if i am dropped?", "I'm dropped, what now", "if I get dropped will I fail",
        )
        probes = policy.dropped_subject_queries(variants[0])
        self.assertTrue(any("attendance" in probe for probe in probes))
        self.assertTrue(any("final rating remarks" in probe for probe in probes))
        self.assertTrue(any("Graduate School" in probe for probe in probes))
        self.assertTrue(any("UAQTEA TES" in probe for probe in probes))
        for question in variants:
            with self.subTest(question=question):
                self.assertIn("dropped_subject", known_query_intents(question))
                self.assertFalse(policy.enrollment_queries(question))
                self.assertFalse(query_needs_history(question))
                self.assertEqual(policy.dropped_subject_queries(question), probes)
                self.assertEqual(resolve_query_intent(question, QueryIntent("enrollment")), question)
                self.assertFalse(query_rewrite_supported(question, "how do I drop a subject", preserve_topic=False))

    def test_voluntary_dropping_and_nonacademic_drops_do_not_receive_the_status_intent(self):
        variants = (
            "how do I drop a subject", "I want to drop", "can I drop after 15 days",
            "how can I get a subject dropped",
            "deadline for dropping, changing and adding subjects",
            "I was dropped off at the gate", "cancel my registration",
            "I want to drop a subject", "how can I have my course dropped",
            "I was not dropped in a subject", "The instructor dropped a pen",
            "I got dropped from the team", "The stock price dropped",
            "Dr. Santos assigned my grade", "What is Dr. Reyes's grading policy?",
            "gusto ko mag-drop ng subject", "mag-drop ako", "na-drop ko ang subject",
            "drop ako", "DR", None, 42, "",
            "I got dropped off at school", "what if I got dropped from the group chat", "I drop", "do I want to get dropped",
            "the call got dropped", "I was dropped by my team",
        )
        for question in variants:
            with self.subTest(question=question):
                self.assertFalse(policy.dropped_subject_queries(question))
                self.assertEqual(resolve_query_intent(question, QueryIntent("dropped_subject")), question)
                if isinstance(question, str):
                    self.assertNotIn("dropped_subject", known_query_intents(question))
                    if question:
                        self.assertFalse(query_rewrite_supported(question, "Dropped subject DR remark", preserve_topic=False))

    def test_registry_keeps_the_previous_handlers_and_adds_payment_terms(self):
        self.assertEqual(set(policy.INTENT_REGISTRY), {"student_id", "dropped_subject", "classroom_location", "payment_terms"})
        for name, question, guidance in (
            ("student_id", "I lost my ID", policy.STUDENT_ID_GUIDANCE),
            ("dropped_subject", "I was dropped from a subject", policy.DROPPED_SUBJECT_GUIDANCE),
            ("classroom_location", "classroom location", policy.CLASSROOM_LOCATION_GUIDANCE),
            ("payment_terms", "Can I pay in installments?", policy.PAYMENT_TERMS_GUIDANCE),
        ):
            with self.subTest(name=name):
                self.assertEqual(set(policy.matching_policy_intents(question)), {name})
                intent = policy.INTENT_REGISTRY[name]
                self.assertEqual(intent.guidance, guidance)
                self.assertTrue(all(isinstance(probe, str) and probe for probe in intent.queries(question)))
                self.assertEqual(decode_optimizer_output(json.dumps({
                    "query": question, "intent": name, "subject": "",
                }), "fallback"), (question, QueryIntent(name)))
        self.assertTrue(policy.INTENT_REGISTRY["student_id"].complete_context)
        self.assertTrue(policy.INTENT_REGISTRY["dropped_subject"].fallback_search)
        self.assertTrue(policy.INTENT_REGISTRY["dropped_subject"].strict_evidence)
        self.assertTrue(policy.INTENT_REGISTRY["classroom_location"].strict_evidence)

    def test_dropped_guidance_scopes_consequences_and_forbids_invented_recovery(self):
        guidance = " ".join(policy.DROPPED_SUBJECT_GUIDANCE.split())
        for required in (
            "maximum 6 accumulated absences", "3 in summer", "Passed, Failed and Dropped",
            "Do not claim exceeding the absence limit automatically causes a Dropped remark",
            "Graduate School Academic Excellence Award", "Academic Achievers Award",
            "collegiate graduation honors", "Academic Achievement Award",
            "Special Citation Award for Commendable Academic Achievement",
            "Academic Scholarship for College", "transferee/health/financial exceptions",
            "Diploma Programs / UAQTEA-TES", "TESDA FY 2022", "more-than-one-dropped-course",
            "One dropped course alone is not that condition", "2024 handbook fallback",
            "do NOT explain how to retake, appeal or recover", "State that verified coverage gap explicitly",
            "Registrar's Office and their college dean", "Do not invent a remedy",
            "Do not substitute voluntary dropping/changing/adding procedures",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guidance)

    def test_classroom_location_and_personal_room_phrasings_keep_their_subject(self):
        for question in (
            "classroom location", "classroom locations", "Where is my classroom?", "my classroom",
            "classroom location of Samar College", "my classroom location at Samar College",
            "what room is my class in", "Which room are my classes in?",
            "saan ang classroom ko?", "diin an akon classroom?", "diin it classroom?",
            "hain an classroom ko?", "ano ang room ng klase ko?",
        ):
            with self.subTest(question=question):
                self.assertIn("classroom_location", known_query_intents(question))
                self.assertFalse(query_needs_history(question))
                self.assertFalse(policy.campus_navigation_queries(question))
                self.assertFalse(policy.campus_location_queries(question))
                self.assertEqual(resolve_query_intent(question, QueryIntent("enrollment")), question)

    def test_dropped_evidence_does_not_promote_corporate_or_doctor_names(self):
        handbook = SimpleNamespace(
            page_content="Academic Excellence Award: a candidate shall have no Incomplete (INC), Dropped (DR), or Failing grade in any subject.",
            metadata={"source": "Samar-College-2024.pdf", "section_heading": "Academic Excellence Award"},
        )
        for text in (
            "Samar Colleges, Inc. - Academic Scholarships\n[SECTION: Academic Scholarships]\nScholarship application forms are available at the Registrar office.",
            "Academic Scholarship Committee: Dr. A. Santos is the chairperson.",
            "Incomplete scholarship application forms have no grades attached.",
        ):
            with self.subTest(text=text):
                incidental = SimpleNamespace(page_content=text, metadata={
                    "source": "Scholarship_Service_2026.docx", "section_heading": "Academic Scholarship Services",
                })
                self.assertEqual(policy.dropped_subject_documents([incidental, handbook]), [handbook])
                selection = policy.select_documents(
                    [incidental, handbook], ["what do i do if i get drop on a subject?"],
                    dates={"samar-college-2024.pdf": {"effective_date": "2024-06-01"}},
                    uploads={"Scholarship_Service_2026.docx": "2026-11-19"}, today=date(2026, 11, 22),
                )
                self.assertEqual(selection.documents, [handbook])
                self.assertFalse(selection.restricted)

    def test_tes_scope_and_each_continuation_condition_survive_separate_chunks(self):
        def passage(text, page, heading):
            return SimpleNamespace(page_content=text, metadata={
                "source": "Samar-College-2024.pdf", "page": page, "section_heading": heading,
            })
        scope = passage("TESDA Circular No. 027, Series 2022. These guidelines shall cover the selection of scholars "
                        "for Diploma Programs under the UAQTEA-TES for FY 2022.", 39, "Scope and Coverage")
        competency = passage("Learners who did not pass or achieve a competent rating in all technology courses "
                             "in the previous semester;", 41, "Prioritization of UAQTEA-TES Scholars")
        dropped = passage("Learners who have more than one dropped course (technology courses and/or academic courses "
                          "including general education courses);", 41, "Prioritization of UAQTEA-TES Scholars")
        self.assertEqual(policy.dropped_subject_documents([scope, competency, dropped]), [scope, competency, dropped])
        coverage = policy.INTENT_REGISTRY["dropped_subject"].coverage
        self.assertEqual(coverage([scope, competency, dropped]), [competency, dropped])
        current_scope = SimpleNamespace(page_content=scope.page_content, metadata={**scope.metadata, "source": "TES_Scope_2026.docx"})
        selection = policy.select_documents(
            [current_scope, dropped], ["I was dropped from a subject"],
            dates={"samar-college-2024.pdf": {"effective_date": "2024-06-01"}},
            uploads={"TES_Scope_2026.docx": "2026-11-19"}, today=date(2026, 11, 22),
        )
        self.assertFalse(selection.restricted)
        self.assertIn(dropped, selection.documents)

    def test_both_optimizer_campus_labels_cannot_rewrite_specific_places(self):
        for question in (
            "classroom location", "my classroom", "what room is my class in",
            "classroom location of Samar College", "library location of Samar College",
            "where is room 101", "where is room 101 for my class?", "Where is room 101 (classroom)?",
            "where is room101 for classes?", "room location", "lab location", "where is the laboratory",
            "gym location", "comfort room location", "restroom location", "building location",
            "floor location", "hall location", "where is the library", "where is the Registrar",
            "where is the gate",
            "classroom preparation schedule",
        ):
            for name in ("campus_navigation", "campus_location"):
                with self.subTest(question=question, intent=name):
                    self.assertEqual(resolve_query_intent(question, QueryIntent(name)), question)
                    self.assertFalse(query_rewrite_supported(question, "I am lost on campus Samar College directions",
                                                            preserve_topic=False))
                    self.assertFalse(query_rewrite_supported(question, "Samar College address", preserve_topic=False))

    def test_actual_lost_speakers_still_use_navigation_even_with_a_room_destination(self):
        for question in ("I am lost on campus", "I am lost on campus looking for my classroom",
                         "I am lost on campus near room 101", "I am lost near the library",
                         "I am lost looking for my classroom", "What if I get lost near room 101?",
                         "nawawara ak ha SC"):
            with self.subTest(question=question):
                self.assertIn("campus_navigation", known_query_intents(question))
                self.assertNotIn("classroom_location", known_query_intents(question))
                self.assertTrue(policy.campus_navigation_queries(question))
                self.assertEqual(resolve_query_intent(question, QueryIntent("campus_location")), question)

    def test_named_rooms_calendar_preparation_and_library_are_not_generic_classrooms(self):
        for question in ("where is room 101", "classroom preparation schedule", "where is the library",
                         "where is room 101 for my class?", "Where is room 101 (classroom)?",
                         "Where is room 101 for classes?", "where is room101 for classes?",
                         "Classroom Preparation May 18 and 25", "classroom rules", "library floors"):
            with self.subTest(question=question):
                self.assertFalse(policy.classroom_location_queries(question))
                self.assertNotIn("classroom_location", known_query_intents(question))
        self.assertEqual(policy.office_location_subject("where is room 101"), "room 101")
        self.assertEqual(policy.office_location_subject("where is room 101 for my class?"), "room 101")
        self.assertEqual(policy.office_location_subject("Where is room 101 (classroom)?"), "room 101")
        self.assertEqual(policy.office_location_subject("where is room101 for classes?"), "room 101")
        self.assertEqual(policy.office_location_subject("where is the library"), "library")

    def test_room_correction_cannot_remove_personal_ownership(self):
        for question in ("my classroom", "what room is my class in", "saan ang classroom ko?"):
            with self.subTest(question=question):
                self.assertFalse(query_rewrite_supported(question, "classroom location"))
                self.assertTrue(query_rewrite_supported(question, "classroom locations", preserve_topic=False))
        self.assertTrue(query_rewrite_supported("what room is my class in", "Where is my classroom?"))

    def test_classroom_guidance_forbids_asking_user_location_and_uses_room_column(self):
        guidance = " ".join(policy.CLASSROOM_LOCATION_GUIDANCE.split())
        for required in (
            "NEVER ask where the user is or their current location",
            "Room column", "consented STUDENT PERSONAL RECORD", "Name the subject and its room",
            "list each enrolled subject", "Blank or N/A Room values are not room assignments",
            "room assignments come from the student's enrollment record",
            "Registrar's Office or their college dean", "Do not demand login or a starting landmark",
            "available documents do not list classroom locations", "Do not guess or use the college address",
            "a library floor", "a classroom-preparation calendar entry",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guidance)

    def test_waray_campus_location_questions_are_equivalent_and_history_independent(self):
        variants = ("diin it sc?", "diin mabibilngan it sc", "Hain an Samar College?",
                    "Where is Samar College located?", "What is the address of Samar Colleges, Inc.?",
                    "SC address", "location of Samar College")
        probes = None
        for question in variants:
            with self.subTest(question=question):
                self.assertIn("campus_location", known_query_intents(question))
                self.assertFalse(query_needs_history(question))
                self.assertEqual(resolve_query_intent(question, QueryIntent("student_id"), [
                    {"role": "user", "content": "I lost my student ID"},
                ]), question)
                actual = policy.campus_location_queries(question)
                probes = probes or actual
                self.assertEqual(actual, probes)
        for question in ("Where is the registrar at SC?", "Where can I replace my ID at SC?",
                         "I am lost at SC", "SC programs", "Where is IT located?"):
            self.assertNotIn("campus_location", known_query_intents(question))

    def test_waray_speaker_lost_at_school_is_navigation_not_a_lost_card(self):
        question = "magaanu man ak kun nawawara ak ha SC HAHAHAHA"
        self.assertIn("campus_navigation", known_query_intents(question))
        self.assertNotIn("student_id", known_query_intents(question))
        self.assertFalse(query_needs_history(question))
        self.assertEqual(resolve_query_intent(question, QueryIntent("student_id"), [
            {"role": "user", "content": "I lost my student ID"},
            {"role": "assistant", "content": "Previous ID answer"},
        ]), question)

    def test_explicit_waray_card_loss_stays_separate_from_lost_on_campus(self):
        for question in ("nawawara an ak student ID ha SC", "nawara an ak RFID", "nawawala ang ID ko"):
            self.assertIn("student_id", known_query_intents(question))
            self.assertNotIn("campus_navigation", known_query_intents(question))

    def test_card_subject_requires_original_or_an_eligible_user_followup_reference(self):
        complete = "magaanu man ak kun nawawara ak ha SC HAHAHAHA"
        wrong = "lost student ID RFID replacement"
        self.assertFalse(query_rewrite_supported(complete, wrong, [{"role": "user", "content": "I lost my student ID"}], preserve_topic=False))
        self.assertFalse(query_rewrite_supported("Nawawara ak yana HAHAHA", wrong, preserve_topic=False))
        self.assertFalse(query_rewrite_supported("what about that?", wrong, [{"role": "assistant", "content": "student ID"}], preserve_topic=False))
        self.assertTrue(query_rewrite_supported("what about that?", wrong, [{"role": "user", "content": "I lost my student ID"}], preserve_topic=False))
        self.assertTrue(query_rewrite_supported("nawawara an ak ID", wrong, preserve_topic=False))

    def test_first_person_lost_id_is_the_same_public_policy_topic_as_a_fragment(self):
        from rag.latency import overview_topic

        variants = ("i lost student ID", "lost student ID", "I lost my student ID.",
                    "my student ID is lost", "How do I replace a lost identification card?")
        for question in variants:
            with self.subTest(question=question):
                self.assertIn("student_id", known_query_intents(question))
                self.assertFalse(query_needs_history(question))
                self.assertEqual(overview_topic(question), "")
                self.assertEqual(resolve_query_intent(question, QueryIntent("enrollment")), question)
                self.assertEqual(policy.student_id_queries(question), policy.student_id_queries("lost student ID"))

    def test_broad_discount_does_not_inherit_family_scope_from_history_or_rewrite(self):
        from rag.source_policy import broad_aid_queries, family_discount_queries

        question = "is there a discount"
        self.assertTrue(broad_aid_queries(question))
        self.assertFalse(family_discount_queries(question))
        self.assertEqual(resolve_query_intent(question, QueryIntent("family_discount"), [
            {"role": "user", "content": "family discount"},
        ]), question)
        self.assertFalse(query_rewrite_supported(question, "family members discount", preserve_topic=False))
        self.assertEqual(resolve_query_intent("ALL", QueryIntent("family_discount"), [
            {"role": "user", "content": "what scholarships are offered?"},
        ]), "what scholarships are offered?")

    def test_grading_query_rewrites_must_preserve_the_explicit_school_level(self):
        question = "is there a graduate school grading?"
        for rewrite in ("Graduate School programs admission history", "college grading system",
                        "elementary grading scale", "fees and scholarships"):
            with self.subTest(rewrite=rewrite):
                self.assertFalse(query_rewrite_supported(question, rewrite, preserve_topic=False))
        self.assertTrue(query_rewrite_supported(question, "graduate grading system scale", preserve_topic=False))
        self.assertTrue(query_rewrite_supported("is there a grading system?", "college grading system",
                                                preserve_topic=False))

    def test_id_policy_intent_is_distinct_from_private_number_lookup_or_lost_other_items(self):
        for question in ("What is my student ID number?", "I lost my student ID number", "I lost my receipt", "I lost my phone",
                         "I lost my diploma", "my grades", "list the students", "student ID issuance for new students"):
            with self.subTest(question=question):
                self.assertNotIn("student_id", known_query_intents(question))

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
                if metadata.name == "leadership":
                    self.assertTrue(resolved.startswith(question + " "), resolved)

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

    def test_amount_followup_carries_family_discount_only_from_recent_user_scope(self):
        history = [{"role": "user", "content": "is there a discount?"},
                   {"role": "user", "content": "for siblings?"}]
        for question in ("show me how much", "How much?", "Could you show me how much it is, please?"):
            with self.subTest(question=question):
                self.assertTrue(query_needs_history(question))
                self.assertIn("discount rates", resolve_query_intent(question, QueryIntent("fees"), history))
        for question, previous in (
            ("How much is the tuition?", history),
            ("show me how much", history + [{"role": "user", "content": "What is the enrollment fee?"}]),
            ("show me how much", [{"role": "assistant", "content": "Sibling discount"}]),
            ("show me how much", [{"role": "user", "content": "Do you offer counseling?"}, history[-1]]),
        ):
            with self.subTest(question=question, previous=previous):
                self.assertEqual(resolve_query_intent(question, history=previous), question)

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
