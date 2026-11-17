"""Offline evidence-boundary tests; no models, cloud clients, or runtime stores."""
import ast
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401

from rag.source_policy import (
    catalog_source_names,
    department_catalog_queries,
    enrollment_queries,
    leadership_guidance,
    newer_source_names,
    program_catalog_queries,
    select_program_affiliation,
    select_documents,
)

BASE = "Samar-College-2024.pdf"
UPDATE = "Samar_Colleges_Inc_Admission_Policy.docx"
TODAY = date(2026, 11, 14)
DATES = {BASE.lower(): {"effective_date": "2024-06-01", "date_source": "document"}}
UPLOADS = {UPDATE: "2026-11-13T02:39:28+00:00"}
LEADERSHIP_QUESTION = "who are the dean of citas and coed also who is the president of samar college"


def document(source=BASE, text="Enrollment requires OLD_CHECKLIST and OLD_FEE.", **metadata):
    return SimpleNamespace(page_content=text, metadata={"source": source, "page": 1, **metadata})


class SourcePolicyTests(unittest.TestCase):
    def test_source_filter_can_be_disabled_without_dropping_explicit_unit_scope(self):
        old = document(text="OLD_RULE from College of Education.", section_heading="College of Education")
        current = document(UPDATE, "CURRENT_RULE from College of Education.", section_heading="College of Education")
        other = document("Other_Current.docx", "OTHER_COLLEGE_RULE", section_heading="College of Engineering")
        before = deepcopy([old, current, other])
        result = select_documents([old, current, other], ["College of Education"], dates=DATES, uploads=UPLOADS,
                                  today=TODAY, limit=10, enforce_sources=False)
        self.assertFalse(result.restricted)
        self.assertEqual(result.documents, [old, current])
        self.assertEqual(result.requested_unit, "College of Education")
        self.assertEqual([old, current, other], before)
        strict = select_documents([old, current, other], ["College of Education"], dates=DATES, uploads=UPLOADS,
                                  today=TODAY, limit=10)
        self.assertTrue(strict.restricted)
        self.assertEqual(strict.documents, [current])

    def test_current_parent_keeps_only_the_degree_declaration_from_old_alias_evidence(self):
        old = document(text="College of Media Studies\nBachelor of Multimedia Arts (BMMA)", section_heading="Programs Offered")
        current = document("Programs_Offered.docx", "College of Information Technology and Allied Sciences\n"
                           "Bachelor of Multimedia Arts CMO No. 15, s. 2017", section_heading="Programs Offered")
        mapping, restricted = select_program_affiliation("dean of BMMA.", [old, current], dates=DATES,
                                                         uploads={current.metadata["source"]: UPLOADS[UPDATE]}, today=TODAY)
        self.assertTrue(restricted)
        self.assertEqual(list(mapping), ["College of Information Technology and Allied Sciences"])
        proofs = next(iter(mapping.values()))
        self.assertIs(proofs[0], current)
        self.assertEqual(proofs[1].page_content, "Bachelor of Multimedia Arts (BMMA)")
        self.assertNotIn("College of Media Studies", str(proofs[1]))
        self.assertEqual(old.page_content, "College of Media Studies\nBachelor of Multimedia Arts (BMMA)")

    def test_program_affiliation_prefers_exact_current_catalog_evidence(self):
        old = document(text="College of Media Studies\nBachelor of Multimedia Arts (BMMA)", section_heading="Programs Offered")
        current = document("Programs_Offered.docx", "College of Information Technology and Allied Sciences\nBachelor of Multimedia Arts (BMMA)",
                           section_heading="Programs Offered")
        mapping, restricted = select_program_affiliation("dean of bmma", [old, current], dates=DATES,
                                                         uploads={current.metadata["source"]: UPLOADS[UPDATE]}, today=TODAY)
        self.assertEqual(mapping, {"College of Information Technology and Allied Sciences": [current]})
        self.assertTrue(restricted)

    def test_unrelated_current_catalog_does_not_prove_program_parent(self):
        other = document("Programs_Offered.docx", "College of Business and Management\nBachelor of Business Administration (BSBA)",
                         section_heading="Programs Offered")
        mapping, restricted = select_program_affiliation("dean of bmma", [other], dates=DATES,
                                                         uploads={other.metadata["source"]: UPLOADS[UPDATE]}, today=TODAY)
        self.assertEqual(mapping, {})
        self.assertFalse(restricted)

    def test_program_parent_current_cohort_conflicts_are_not_guessed(self):
        docs = [document("Media_Update.docx", "College of Media Studies\nBachelor of Multimedia Arts (BMMA)", section_heading="Programs Offered"),
                document("Arts_Update.docx", "College of Liberal Arts and Sciences\nBachelor of Multimedia Arts (BMMA)", section_heading="Programs Offered")]
        mapping, restricted = select_program_affiliation("dean of bmma", docs, dates=DATES,
                                                         uploads={doc.metadata["source"]: UPLOADS[UPDATE] for doc in docs}, today=TODAY)
        self.assertEqual(set(mapping), {"College of Media Studies", "College of Liberal Arts and Sciences"})
        self.assertTrue(restricted)

    def test_historical_program_parent_is_preserved_but_not_called_current(self):
        old = document(text="College of Media Studies\nBachelor of Multimedia Arts (BMMA)", section_heading="Programs Offered")
        current = document("Programs_Offered_2025.docx", "College of Information Technology and Allied Sciences\nBachelor of Multimedia Arts (BMMA)",
                           section_heading="Programs Offered")
        mapping, restricted = select_program_affiliation("dean of BMMA in 2024", [old, current], dates=DATES,
                                                         uploads={current.metadata["source"]: UPLOADS[UPDATE]}, today=TODAY)
        self.assertEqual(mapping, {"College of Media Studies": [old]})
        self.assertFalse(restricted)

    def select(self, docs, query="What do I need to enroll?", **kwargs):
        values = {"dates": DATES, "uploads": UPLOADS, "today": TODAY}
        values.update(kwargs)
        with patch("rag.freshness.get_doc_dates", side_effect=AssertionError("No store reads")):
            return select_documents(docs, query, **values)

    def test_current_undated_upload_excludes_every_handbook_page(self):
        current = document(UPDATE, "Enrollment now uses CURRENT_ONLINE_APPLICATION only.", section="Admission Policy")
        old = [document(page=i) for i in range(1, 5)]
        result = self.select([*old, current])
        self.assertTrue(result.restricted)
        self.assertEqual(result.documents, [current])
        self.assertEqual(result.discarded_sources, (BASE.lower(),))
        self.assertNotIn("OLD_CHECKLIST", " ".join(doc.page_content for doc in result.documents))

    def test_selection_occurs_before_final_context_limit(self):
        current = document(UPDATE, "Enrollment uses the new online form.")
        result = self.select([*(document(page=i) for i in range(25)), current], limit=2)
        self.assertEqual(result.documents, [current])

    def test_all_complementary_current_uploads_are_retained(self):
        programs = document("Programs_Offered.docx", "Programs offered: BSIT and BSBA.", section="Course Offering")
        shs = document("senior_high_program.docx", "Senior high school program: STEM and ABM strands.")
        legacy = document("Samar-College-update.pdf", "Programs offered: OLD_PROGRAM.")
        result = self.select([document(), shs, legacy, programs], "What programs are offered?", uploads={
            programs.metadata["source"]: "2026-11-13T02:39:20Z",
            shs.metadata["source"]: "2026-11-13T02:39:33Z",
        }, dates={**DATES, "samar-college-update.pdf": {"effective_date": "2025-06-01"}})
        self.assertEqual(result.documents, [shs, programs])
        self.assertIn("samar-college-update.pdf", result.discarded_sources)

    def test_current_catalog_does_not_resurrect_an_omitted_program(self):
        current = document("Programs_Offered.docx", "Course Offering\nBSIT and BSBA are listed.", section="Course Offering")
        result = self.select([document(text="BA Sociology was offered in the old program list."), current],
                             "Is BA Sociology offered?", uploads={current.metadata["source"]: UPLOADS[UPDATE]})
        self.assertEqual(result.documents, [current])

    def test_short_offering_requests_select_the_current_catalog_not_old_training(self):
        source = "Samar_college_Inc_Programs_offered.docx"
        current = document(source, "College of Information Technology and Allied Sciences\n"
                           "Bachelor of Science in Information Technology",
                           collection_heading="Programs Offered")
        old = document(text="SCTI Training Offerings\nCookery NC II\nOLD_TRAINING_ENTRY")
        notice = document("Library_Notice.docx", "Confirmed current library operating hours: eight to five.")
        for query in ("confirmed current offerings", "current offerings", "latest offerings",
                      "program offerings", "what courses are currently offered?"):
            with self.subTest(query=query):
                result = self.select([old, notice, current], query, uploads={
                    source: UPLOADS[UPDATE], notice.metadata["source"]: UPLOADS[UPDATE],
                })
                self.assertTrue(result.restricted)
                self.assertEqual(result.documents, [current])
                self.assertIn(BASE.lower(), result.discarded_sources)

    def test_current_active_colleges_are_catalog_requests_not_proof_old_units_are_active(self):
        old = document(text="College of Arts and Sciences\nOLD_COLLEGE_LIST")
        current = document("Programs_Offered.docx", "College of River Studies\nBachelor of River Studies",
                           collection_heading="Programs Offered")
        for query in ("currently active colleges", "the currently active colleges instead",
                      "share details on the currently active colleges instead", "list current colleges"):
            with self.subTest(query=query):
                self.assertEqual(len(department_catalog_queries(query)), 2)
                result = self.select([old, current], query, uploads={"Programs_Offered.docx": UPLOADS[UPDATE]})
                self.assertEqual(result.documents, [current])
                self.assertTrue(result.restricted)
                missing = self.select([old], query, uploads={})
                self.assertEqual(missing.documents, [])
                self.assertFalse(missing.restricted)

    def test_confirmed_current_offerings_cannot_fall_back_to_unverified_old_lists(self):
        old = document(text="SCTI Training Offerings\nCookery NC II\nOLD_TRAINING_ENTRY")
        for body, uploads in (("Programs Offered 2026", {"Programs_Offered.docx": UPLOADS[UPDATE]}),
                              ("Programs Offered\nSee the handbook for the list.", {"Programs_Offered.docx": UPLOADS[UPDATE]}),
                              ("Programs offered: Bachelor of River Studies.", {})):
            with self.subTest(body=body, uploads=uploads):
                current = document("Programs_Offered.docx", body, collection_heading="Programs Offered")
                result = self.select([old, current], "confirmed current offerings", uploads=uploads)
                self.assertEqual(result.documents, [])
                self.assertFalse(result.restricted, "Do not claim a verified current source was found")
        self.assertEqual(self.select([old], "current offerings", uploads={}).documents, [])

    def test_current_offering_request_keeps_explicit_unit_identity(self):
        old = document(text="College of Arts and Sciences\nBachelor of OLD_CAS_FIELD",
                       section_heading="College of Arts and Sciences")
        other = document("Programs_Offered.docx", "College of Liberal Arts and Sciences\n"
                         "Bachelor of CURRENT_CLAS_FIELD", collection_heading="Programs Offered")
        uploads = {other.metadata["source"]: UPLOADS[UPDATE]}
        current = self.select([old, other], "Current offerings for College of Arts and Sciences", uploads=uploads)
        self.assertEqual(current.documents, [])
        self.assertEqual(current.requested_unit, "College of Arts and Sciences")
        self.assertEqual(self.select([old, other], "cas", uploads=uploads).documents, [old])

    def test_historical_offering_requests_still_keep_requested_editions(self):
        old = document(text="Programs offered: OLD_PROGRAM.")
        current = document("Programs_Offered.docx", "Programs offered: CURRENT_PROGRAM.")
        for query in ("What programs were offered in 2024?", "Compare the 2024 handbook and current offerings"):
            with self.subTest(query=query):
                result = self.select([old, current], query, uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                self.assertEqual(result.documents, [old, current])
                self.assertFalse(result.restricted)

    def test_institution_history_keeps_handbook_narrative_not_history_subjects(self):
        old = document(text="THE HISTORICAL ACCOUNT OF SAMAR COLLEGE\n"
                       "Our school opened its doors on July 1, 1949 under the name of Samar Junior College.",
                       section_heading="THE HISTORICAL ACCOUNT OF SAMAR COLLEGE", page=8)
        course = document("Curriculum.docx", "Course Code | Subject | Units\n"
                          "GE 2 | Readings in Philippine History | 3\nFirst year general education subjects.",
                          section_heading="First Year / General Education", collection_heading="CITAS Curriculum")
        newer_history = document("College_History.docx", "The school expanded into its next campus building, "
                                 "as described in the documented institutional timeline.", section_heading="Institutional History")
        uploads = {course.metadata["source"]: UPLOADS[UPDATE], newer_history.metadata["source"]: UPLOADS[UPDATE]}
        for query in ("history of samar college", "Historical background of Samar College",
                      "When was Samar College established?", "Who founded Samar College?"):
            with self.subTest(query=query):
                result = self.select([course, old, newer_history], query, uploads=uploads)
                self.assertEqual(result.documents, [old, newer_history])
                self.assertFalse(result.restricted, "Older historical facts are not superseded by a newer curriculum")

    def test_history_heading_cross_reference_or_course_list_is_not_historical_evidence(self):
        for body, heading in (
            ("History of Samar College", "History of Samar College"),
            ("Historical account\nSee the handbook for the full story.", "Historical Account"),
            ("Table of contents\nHistorical Account..........8\nStudent Services..........30", "Contents"),
            ("HIST 101 | History of Education | 3 units\nA required first year subject.", "History Curriculum"),
            ("The department offers Readings in Philippine History as a three unit subject.", "Course Offerings"),
            ("The current school officials and departments are shown in this organizational structure.", "Organization"),
            ("A faculty member graduated from another university, founded many years before joining this school.", "Faculty Qualifications"),
        ):
            with self.subTest(body=body):
                unrelated = document("Current.docx", body, section_heading=heading)
                self.assertEqual(self.select([unrelated], "history of samar college",
                                             uploads={"Current.docx": UPLOADS[UPDATE]}).documents, [])

    def test_institution_history_intent_is_not_history_classes_or_student_records(self):
        from rag.source_policy import institution_history_queries

        for query in ("history of samar college", "River University historical background",
                      "The origins of North Academy", "Who founded the school?", "When did the college open?"):
            with self.subTest(query=query):
                probes = institution_history_queries(query)
                self.assertEqual(len(probes), 2)
                self.assertTrue(all(probe.startswith(query + " ") for probe in probes))
        for query in ("Current History courses at Samar College", "My enrollment history at the college",
                      "What grade did I receive in History at the school?", "Who is the dean of the History Department?",
                      "What programs does the Department of History offer?", "current offerings", "", None):
            with self.subTest(query=query):
                self.assertEqual(institution_history_queries(query), [])

    def test_institution_history_respects_an_explicit_handbook_exclusion(self):
        old = document(text="The school opened its doors to its first students as described in this historical account.",
                       section_heading="Historical Account")
        recent = document("College_History.docx", "The college expanded into its next campus building in the documented timeline.",
                          section_heading="Institutional History")
        result = self.select([old, recent], "Ignore the 2024 handbook. History of Samar College",
                             uploads={"College_History.docx": UPLOADS[UPDATE]})
        self.assertEqual(result.documents, [recent])

    def test_reference_source_hints_keep_registered_paths_and_case(self):
        from rag.source_policy import history_source_names

        archived = "Archive/Student_Handbook_2024.PDF"
        history = "School_Historical_Account.DOCX"
        hints = history_source_names(dates={
            "historical_account.docx": {"filename": history},
            BASE.lower(): {"effective_date": "2024-01-01"},
        }, uploads={archived: UPLOADS[UPDATE], "CITAS_Curriculum.docx": UPLOADS[UPDATE]})
        self.assertIn(archived, hints)
        self.assertIn(history, hints)
        self.assertIn(BASE, hints)
        self.assertNotIn("CITAS_Curriculum.docx", hints)
        self.assertEqual(len(hints), len(set(hints)))

    def test_catalog_cannot_replace_uncovered_fee_guidance(self):
        current = document("Programs_Offered.docx", "Course Offering\nBSIT and BSBA are listed.", section="Course Offering")
        old = document(text="BSIT tuition fee is OLD_FEE.")
        result = self.select([old, current], "What are the tuition fees for BSIT?",
                             uploads={current.metadata["source"]: UPLOADS[UPDATE]})
        self.assertFalse(result.restricted)
        self.assertIn(old, result.documents)

    def test_bsit_enrollment_uses_shared_guidance_not_a_current_program_catalog(self):
        current = document("Programs_Offered.docx", "College of Business and Management\nBachelor of Science in Business Administration",
                           collection_heading="Programs Offered")
        guide = document(text="ENROLLMENT PROCEDURE\nNew Students and Transferees\n"
                              "Submit credentials and secure the plotting form. Present the receipt to the Registrar.",
                         section_heading="Enrollment Procedure")
        for query in ("how to enroll in bsit", "How to enrol in the BSIT program?", "BSIT admission requirements"):
            with self.subTest(query=query):
                result = self.select([current, guide], [query, "BSIT enrollment admission program requirements"],
                                     uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                self.assertEqual(result.documents, [guide])
                self.assertFalse(result.restricted)
                self.assertIn(current.metadata["source"].lower(), result.discarded_sources)
                self.assertEqual(program_catalog_queries(query), [])

    def test_current_enrollment_update_still_replaces_shared_old_steps(self):
        current = document(UPDATE, "Undergraduate Level\nEnrollment now uses CURRENT_ONLINE_APPLICATION only.",
                           section_heading="Admission Policy")
        old = document(text="Enrollment requires OLD_CHECKLIST and OLD_FEE.", section_heading="Enrollment Procedure")
        result = self.select([old, current], "how to enroll in bsit")
        self.assertEqual(result.documents, [current])
        self.assertTrue(result.restricted)

    def test_shared_undergraduate_enrollment_keeps_mixed_page_but_not_other_units_or_levels(self):
        shared = document(text="ADMISSION REQUIREMENTS\nUndergraduate Level\nStudents submit valid credentials.\n"
                               "Graduate Level\nEducation applicants need the stated graduate-level average.",
                          section_heading="Graduate Level")
        shs = document("SHS_Admission.docx", "Admission requires SHS_NEW_DOCUMENTS.", section_heading="Senior High School Admission")
        graduate = document("Graduate_Admission.docx", "Admission requires GRADUATE_DEGREE.", section_heading="Graduate Level")
        business = document("Business_Admission.docx", "College of Business and Management\nEnrollment requires BUSINESS_ONLY_FORM.",
                            section_heading="College of Business and Management")
        technical = document("Diploma_Admission.docx", "Admission to diploma programs requires TESDA_ONLY_DOCUMENTS.",
                             section_heading="UAQTEA-TES Admission")
        result = self.select([shs, graduate, business, technical, shared], "how to enroll in bsit", uploads={
            shs.metadata["source"]: UPLOADS[UPDATE], graduate.metadata["source"]: UPLOADS[UPDATE],
            business.metadata["source"]: UPLOADS[UPDATE], technical.metadata["source"]: UPLOADS[UPDATE],
        })
        self.assertEqual(result.documents, [shared])
        self.assertFalse(result.restricted)

    def test_explicit_college_can_use_shared_enrollment_rules_without_becoming_an_alias(self):
        guide = document(text="Enrollment procedure: submit credentials and secure the plotting form.",
                         section_heading="General College Enrollment")
        other = document("Other_Admission.docx", "College of Business and Management\nEnrollment uses BUSINESS_ONLY_FORM.",
                         section_heading="College of Business and Management")
        query = "How to enroll in College of Information Technology and Allied Sciences?"
        result = self.select([other, guide], query, uploads={other.metadata["source"]: UPLOADS[UPDATE]})
        self.assertEqual(result.documents, [guide])
        self.assertEqual(result.requested_unit, "College of Information Technology and Allied Sciences")

    def test_enrollment_and_offering_parts_keep_both_requested_kinds_of_evidence(self):
        guide = document(text="Enrollment procedure: submit credentials to the registrar.", section_heading="Enrollment Procedure")
        catalog = document(text="Programs offered: BSIT and BSBA are documented here.", collection_heading="Programs Offered")
        result = self.select([guide, catalog], ["What programs are offered and how do I enroll?", "What programs are offered?", "How do I enroll?"],
                             uploads={})
        self.assertEqual(result.documents, [guide, catalog])

    def test_programs_i_can_enroll_in_still_requests_offerings_not_steps(self):
        catalog = document("Programs_Offered.docx", "Programs offered: BSIT and BSBA.", collection_heading="Programs Offered")
        for query in ("What programs can I enroll in?", "Which courses may students enrol in?"):
            with self.subTest(query=query):
                self.assertEqual(enrollment_queries(query), [])
                self.assertTrue(program_catalog_queries(query))
                result = self.select([document(), catalog], query, uploads={catalog.metadata["source"]: UPLOADS[UPDATE]})
                self.assertEqual(result.documents, [catalog])

    def test_unrelated_newer_documents_do_not_disable_fallback(self):
        for text, query in (
            ("Latin honors requirements include a qualifying grade average.", "What is the enrollment procedure?"),
            ("Admission requires school records. Honor students may request a discount.", "What are Latin honors requirements?"),
            ("The academic calendar lists the final examination dates.", "Who is the dean of CITAS?"),
        ):
            with self.subTest(query=query):
                new = document("Campus_Notice.docx", text)
                old = document()
                result = self.select([old, new], query, uploads={new.metadata["source"]: UPLOADS[UPDATE]})
                self.assertFalse(result.restricted)
                self.assertEqual(result.documents, [old] if enrollment_queries(query) else [old, new])

    def test_empty_titles_indexes_and_cross_references_are_not_replacements(self):
        for text in ("", "  ", "Enrollment procedure", "Enrollment policy update",
                     "Table of contents\nEnrollment........4\nFees........5",
                     "Enrollment procedure\nSee the 2024 handbook for the complete instructions."):
            with self.subTest(text=text):
                result = self.select([document(), document(UPDATE, text)])
                self.assertFalse(result.restricted)

    def test_current_organizational_roster_covers_deans_and_officials(self):
        roster = document("Organizational_Structure_v2.docx", "Dean of CITAS: Dr. Current Person. Principal: Ms. Current Head.")
        result = self.select([document(text="Dean and officials: OLD_ROSTER."), roster],
                             "Who are the college deans and school officials?",
                             uploads={roster.metadata["source"]: UPLOADS[UPDATE]})
        self.assertEqual(result.documents, [roster])

    def test_catalog_heading_or_cross_reference_does_not_prove_coverage(self):
        for body in ("Programs Offered 2026", "Course Offering\nSee the handbook for the full list of degrees."):
            with self.subTest(body=body):
                current = document("Programs_Offered.docx", body, section="Course Offering")
                result = self.select([document(), current], "Is Sociology offered?",
                                     uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                self.assertFalse(result.restricted)

    def test_a_different_department_roster_cannot_replace_the_requested_dean(self):
        roster = document("Education_Roster.docx", "Dean of CoEd: Dr. Current Person.")
        result = self.select([document(text="Dean of CITAS: Dr. Previous Person."), roster],
                             "Who is the dean of CITAS?", uploads={roster.metadata["source"]: UPLOADS[UPDATE]})
        self.assertFalse(result.restricted)

    def test_current_dean_update_name_dash_unit_row_is_substantive_coed_evidence(self):
        source = "Synthetic_Leadership_2025-2026.pdf"
        row = "Synthetic Name \u2013 College of Education"
        current = document(source, "DEANS UPDATE\n" + row, page=15, section_heading="DEANS UPDATE")
        old = document(text="Dr. Synthetic Historical Official\nDean, College of Education", page=26,
                       section_heading="College of Education")
        before = deepcopy([old, current])
        for query, unit in (("Who is the dean of coed?", "coed"),
                            ("Who is the dean of College of Education?", "College of Education")):
            with self.subTest(query=query):
                result = self.select([old, current], query, uploads={source: UPLOADS[UPDATE]})
                self.assertTrue(result.restricted)
                self.assertEqual(result.requested_unit, unit)
                self.assertEqual(result.documents, [current])
                self.assertIs(result.documents[0], current)
                self.assertIn(row, result.documents[0].page_content)
                self.assertEqual(result.documents[0].metadata["page"], 15)
                self.assertIn(BASE.lower(), result.discarded_sources)
        self.assertEqual([old, current], before)

    def test_other_college_dean_update_rows_cannot_masquerade_as_coed(self):
        old = document(text="Dr. Synthetic Historical Official\nDean, College of Education",
                       section_heading="College of Education")
        for unit in ("College of Information Technology & Allied Sciences", "College of Graduate Studies",
                     "College of Special Education", "College of Basic Education", "College of Education and Technology"):
            for query in ("Who is the dean of coed?", "Who is the dean of College of Education?"):
                with self.subTest(unit=unit, query=query):
                    source = "Synthetic_Other_Roster_2025-2026.pdf"
                    current = document(source, f"DEANS UPDATE\nSynthetic Other Official \u2013 {unit}",
                                       section_heading="DEANS UPDATE", page=15)
                    before = deepcopy(current)
                    result = self.select([current, old], query, uploads={source: UPLOADS[UPDATE]})
                    self.assertFalse(result.restricted)
                    self.assertEqual(result.documents, [old])
                    self.assertIn(source.lower(), result.discarded_sources)
                    self.assertEqual(current, before)

    def test_same_current_source_preserves_named_update_and_conflicting_multi_unit_profile(self):
        source = "Synthetic_Leadership_2025-2026.pdf"
        update = document(source, "DEANS UPDATE\n"
                          "Synthetic Meadow Official \u2013 College of Education\n"
                          "Synthetic River Official \u2013 College of Information Technology & Allied Sciences\n"
                          "Synthetic Valley Official \u2013 College of Graduate Studies",
                          section_heading="DEANS UPDATE", page=15, chunk_id="update")
        profile = document(source, "Dr. Synthetic Valley Official\n"
                           "Dean, College of Graduate Studies & College of Education",
                           section_heading="College Profile", page=26, chunk_id="profile")
        other_row = document(source, "DEANS UPDATE\nSynthetic River Official \u2013 "
                             "College of Information Technology & Allied Sciences", section_heading="DEANS UPDATE",
                             page=15, chunk_id="other")
        old = document(text="Dr. Synthetic Historical Official\nDean, College of Education",
                       section_heading="College of Education", chunk_id="historical")
        docs = [other_row, old, update, profile]
        before = deepcopy(docs)
        result = self.select(docs, "Who is the dean of coed?", uploads={source: UPLOADS[UPDATE]})
        self.assertTrue(result.restricted)
        self.assertEqual(result.documents, [update, profile])
        self.assertEqual([doc.metadata["page"] for doc in result.documents], [15, 26])
        self.assertTrue(all(doc.metadata["source"] == source for doc in result.documents))
        self.assertIn("Synthetic Meadow Official", result.documents[0].page_content)
        self.assertIn("Dr. Synthetic Valley Official", result.documents[1].page_content)
        self.assertIn(BASE.lower(), result.discarded_sources)
        self.assertEqual(docs, before)

    def test_exact_compound_leadership_scope_keeps_three_current_sources_not_a_contextual_single_unit(self):
        current = [
            document("Synthetic_Citas_Roster_2025-2026.pdf", "DEANS UPDATE\n"
                     "Synthetic River Official \u2013 College of Information Technology & Allied Sciences (CITAS)",
                     section_heading="DEANS UPDATE"),
            document("Synthetic_Education_Roster_2025-2026.pdf", "DEANS UPDATE\n"
                     "Synthetic Meadow Official \u2013 College of Education", section_heading="DEANS UPDATE"),
            document("Synthetic_President_Roster_2025-2026.pdf", "President of Samar College: Synthetic Harbor Official.",
                     section_heading="School Officials"),
        ]
        old = document(text="Dean of CoEd: Synthetic Historical Official. President: Synthetic Previous Official.")
        aspects = ["who are the dean of citas", "who are the dean of coed", "who is the president of samar college",
                   "who are the dean of College of Education"]
        uploads = {doc.metadata["source"]: UPLOADS[UPDATE] for doc in current}
        before = deepcopy([old, *current])
        for contextual in ([], ["Who is the dean of CITAS at Samar College?"]):
            with self.subTest(contextual=contextual):
                result = self.select([old, *current], [LEADERSHIP_QUESTION, *contextual, *aspects], uploads=uploads)
                self.assertTrue(result.restricted)
                self.assertEqual(result.requested_unit, "")
                self.assertEqual(result.documents, current)
                self.assertIn(BASE.lower(), result.discarded_sources)
        self.assertEqual([old, *current], before)

    def test_named_leadership_guidance_requires_conflict_disclosure_not_unavailable_or_old_substitution(self):
        for query in (LEADERSHIP_QUESTION, "Who is the dean of coed?", "Who is the president of Samar College?",
                      "List college dean names", "dean of BMMA", "deans of BMMA and BEED?"):
            with self.subTest(query=query):
                guidance = leadership_guidance(query)
                self.assertTrue(guidance.startswith("<leadership_roster_request>"))
                self.assertTrue(guidance.endswith("</leadership_roster_request>"))
                normalized = " ".join(guidance.split())
                self.assertIn("Answer each requested role and college separately", normalized)
                self.assertIn("A dean-update heading can label name-to-college rows", normalized)
                self.assertIn("cite and describe that conflict rather than claiming the information is unavailable", normalized)
                self.assertIn("asserting an unverified current office holder", normalized)
                self.assertIn("Do not use an old appointment or handbook to resolve a current-source conflict.", normalized)
        for query in ("What does a college dean do?", "What are the president's responsibilities?",
                      "College of Education programs offered", "dean of CoEd responsibilities", "", None):
            with self.subTest(query=query):
                self.assertEqual(leadership_guidance(query), "")

    def test_partial_update_does_not_pull_old_steps_back_in(self):
        current = document(UPDATE, "Enrollment begins by submitting the CURRENT_FORM online.")
        result = self.select([document(text="Enrollment steps include OLD_REQUIREMENT_1 and OLD_REQUIREMENT_2."), current])
        self.assertTrue(result.restricted)
        self.assertEqual(result.documents, [current])

    def test_known_old_and_future_documents_do_not_win_by_upload_recency(self):
        for effective in ("2019-06-01", "2027-06-01"):
            with self.subTest(effective=effective):
                result = self.select([document(), document(UPDATE, "Enrollment uses an online application.")],
                                     dates={**DATES, UPDATE.lower(): {"effective_date": effective}})
                self.assertFalse(result.restricted)

    def test_missing_chronology_does_not_invent_a_date(self):
        current = document(UPDATE, "Enrollment uses an online application.")
        for uploads in ({}, {UPDATE: "invalid-date"}, {UPDATE: "2023-01-01T00:00:00Z"}):
            with self.subTest(uploads=uploads):
                result = self.select([document(), current], uploads=uploads)
                self.assertFalse(result.restricted)

    def test_dated_update_works_without_upload_metadata(self):
        current = document("Policy-2026.pdf", "Enrollment uses the CURRENT_FORM online.")
        result = self.select([document(), current], uploads={})
        self.assertTrue(result.restricted)
        self.assertEqual(result.documents, [current])

    def test_equal_dates_and_equal_upload_times_do_not_invent_precedence(self):
        current = document("Policy-2024.docx", "Enrollment uses an online application.")
        for uploads in ({}, {BASE: UPLOADS[UPDATE], current.metadata["source"]: UPLOADS[UPDATE]}):
            with self.subTest(uploads=uploads):
                result = self.select([document(), current], uploads=uploads)
                self.assertFalse(result.restricted)

    def test_recorded_dates_override_filename_years(self):
        current = document("Policy-2026.pdf", "Enrollment uses an online application.")
        result = self.select([document(), current], uploads={}, dates={
            **DATES, "policy-2026.pdf": {"effective_date": "2019-01-01", "date_source": "admin"},
        })
        self.assertFalse(result.restricted)

    def test_historical_and_comparison_questions_keep_requested_versions(self):
        documents = [document(), document(UPDATE, "Enrollment uses the CURRENT_FORM online.")]
        for query in ("What was the enrollment procedure in 2024?", "According to the 2024 handbook, how do I enroll?",
                      "According to Samar-College-2024.pdf, how do I enroll?",
                      "Compare Samar-College-2024.pdf with the current document",
                      "Compare the 2024 and 2026 enrollment procedures", "What changed in enrollment?"):
            with self.subTest(query=query):
                result = self.select(documents, query)
                self.assertFalse(result.restricted)
                self.assertEqual(result.documents, documents)

    def test_ignore_old_handbook_is_not_a_historical_request(self):
        current = document(UPDATE, "Enrollment uses the CURRENT_FORM online.")
        result = self.select([document(), current], "Ignore the 2024 handbook and use the current enrollment procedure")
        self.assertTrue(result.restricted)

    def test_graduation_year_does_not_turn_current_question_into_history(self):
        current = document(UPDATE, "Enrollment uses the CURRENT_FORM online.")
        result = self.select([document(), current],
                             ["I graduated in 2024. What is the current enrollment procedure?", "enrollment procedure"])
        self.assertTrue(result.restricted)

    def test_path_case_aliases_and_renamed_2024_handbook(self):
        old = document(r"C:\uploads\STUDENT-Handbook-2024.PDF")
        current = document("s3://bucket/docs/ADMISSION-POLICY.docx", "Enrollment uses the CURRENT_FORM online.")
        result = self.select([old, current], uploads={"admission-policy.docx": UPLOADS[UPDATE]})
        self.assertEqual(result.documents, [current])
        self.assertIn("student-handbook-2024.pdf", result.discarded_sources)

    def test_current_upload_only_still_protects_against_stale_history(self):
        current = document(UPDATE, "Enrollment uses the CURRENT_FORM online.")
        self.assertTrue(self.select([current]).restricted)

    def test_ordinary_unrelated_sources_keep_existing_selection(self):
        docs = [document("Calendar-2026.pdf", "Classes begin in November."),
                document("Notice-2026.pdf", "The office is closed on Monday.")]
        result = self.select(docs, "When do classes begin?", uploads={})
        self.assertFalse(result.restricted)
        self.assertEqual(result.documents, docs)

    def test_inputs_are_not_modified_and_order_and_limit_are_preserved(self):
        docs = [document(), *(document(UPDATE, f"Enrollment uses CURRENT_FORM step {i} online.", chunk_id=str(i)) for i in range(3))]
        before, dates, uploads = deepcopy(docs), deepcopy(DATES), deepcopy(UPLOADS)
        result = self.select(docs, limit=2)
        self.assertEqual(result.documents, docs[1:3])
        self.assertEqual(docs, before)
        self.assertEqual(DATES, dates)
        self.assertEqual(UPLOADS, uploads)

    def test_source_cannot_supersede_itself(self):
        docs = [document(), document("s3://bucket/SAMAR-COLLEGE-2024.PDF", "Enrollment uses another page of the same handbook.")]
        result = self.select(docs, uploads={BASE: UPLOADS[UPDATE]})
        self.assertFalse(result.restricted)

    def test_department_list_uses_current_catalog_without_literal_department(self):
        current = document("Programs_Offered.docx", "College of River Studies\nBachelor of River Studies",
                           collection_heading="Programs Offered", section_heading="College of River Studies")
        old = document(text="College departments: OLD_UNIT and OMITTED_OLD_UNIT.")
        for query in ("college department", "academic department", "What are the college departments?"):
            with self.subTest(query=query):
                result = self.select([old, current], query, uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                self.assertTrue(result.restricted)
                self.assertEqual(result.documents, [current])
                self.assertIs(result.documents[0], current)

    def test_department_alias_also_preserves_specific_contact_relevance(self):
        current = document("Contacts.docx", "River departments contact: CURRENT_PHONE.")
        result = self.select([document(), current], "Who is the contact for River department?",
                             uploads={current.metadata["source"]: UPLOADS[UPDATE]})
        self.assertEqual(result.documents, [current])

    def test_department_catalog_uses_all_heading_metadata_and_named_departments(self):
        for key in ("section_heading", "collection_heading", "section", "header"):
            for name in ("College of River Studies", "River Studies Department", "Department of River Studies"):
                with self.subTest(key=key, name=name):
                    current = document("Offerings_Update.docx", "Bachelor of River Studies", **{key: name})
                    result = self.select([document(), current], "List the academic departments",
                                         uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                    self.assertEqual(result.documents, [current])
                    self.assertEqual(current.metadata[key], name)

    def test_substantive_catalog_keeps_all_unit_chunks_before_rosters_and_curricula(self):
        source = "Programs_Offered.DOCX"
        river = document(source, "Bachelor of River Studies", section_heading="College of River Studies",
                         collection_heading="Programs Offered", chunk_id="river")
        valley = document(source, "Bachelor of Valley Studies", section_heading="College of Valley Studies",
                          collection_heading="Programs Offered", chunk_id="valley")
        third = document(source, "Bachelor of Forest Studies", section_heading="Forest Studies Department",
                         collection_heading="College Course Offerings", chunk_id="forest")
        roster = document("Staff_2026.docx", "College of River Studies | Dean: Current Person\n"
                          "College of OLD_ROSTER_UNIT | Dean: Other Person", section_heading="College Directory")
        curriculum = document("Curriculum.docx", "College of River Studies\nYear 1: field methods and data analysis")
        unknown = document("Legacy_Program_Catalog.pdf", "College of UNKNOWN_OLD_UNIT\nBachelor of Old Studies")
        shs = document(source, "English Department | Teacher: Current Person",
                       section_heading="SHS Academic Departments", collection_heading="Programs Offered")
        stub = document(source, "College of OMITTED_UNIT\nRefer to the handbook for the list of programs.",
                        collection_heading="Programs Offered")
        bare = document(source, "Programs Offered 2026", collection_heading="Programs Offered")
        docs = [document(), roster, unknown, curriculum, shs, valley, bare, river, stub, third]
        before = deepcopy(docs)
        uploads = {name: UPLOADS[UPDATE] for name in (source, roster.metadata["source"], curriculum.metadata["source"])}
        result = self.select(docs, "What are the college departments?", uploads=uploads)
        self.assertTrue(result.restricted)
        self.assertEqual(result.documents, [valley, river, third])
        self.assertEqual(result.discarded_sources,
                         ("curriculum.docx", "legacy_program_catalog.pdf", BASE.lower(), "staff_2026.docx"))
        self.assertEqual(self.select(docs, "academic department", uploads=uploads, limit=2).documents, [valley, river])
        self.assertEqual(docs, before)
        for actual, original in zip(result.documents, [valley, river, third]):
            self.assertIs(actual, original)

    def test_complementary_current_department_catalogs_remain_in_input_order(self):
        first = document("Programs_Offered.docx", "College of River Studies\nBachelor of River Studies")
        second = document("Course_Catalog.docx", "College of Valley Studies\nBachelor of Valley Studies")
        result = self.select([document(), second, first], "Which academic departments are available?",
                             uploads={first.metadata["source"]: "2026-11-13T02:39:20Z",
                                      second.metadata["source"]: "2026-11-13T02:39:33Z"})
        self.assertEqual(result.documents, [second, first])

    def test_structural_college_unit_list_need_not_have_a_catalog_filename(self):
        current = document("Academic_Update.docx", "College departments\n1. College of River Studies\n"
                           "2. College of Valley Studies")
        result = self.select([document(), current], "List the college departments",
                             uploads={current.metadata["source"]: UPLOADS[UPDATE]})
        self.assertTrue(result.restricted)
        self.assertEqual(result.documents, [current])

    def test_named_catalog_rows_are_evidence_even_when_repeated_in_metadata(self):
        for name in ("College of River Studies", "Department of River Studies", "River Studies Department"):
            with self.subTest(name=name):
                current = document("Programs_Offered.docx", name, section_heading=name,
                                   collection_heading="College Programs Offered")
                result = self.select([document(), current], "List the academic departments",
                                     uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                self.assertEqual(result.documents, [current])

    def test_bare_catalog_body_is_not_substantive_unit_coverage_from_metadata(self):
        for body in ("Programs Offered 2026", "Course Offering", "Academic Catalog", "Academic Departments"):
            with self.subTest(body=body):
                current = document("Programs_Offered.docx", body, section_heading="College of River Studies")
                self.assertFalse(self.select([document(), current], "academic department",
                                             uploads={current.metadata["source"]: UPLOADS[UPDATE]}).restricted)

    def test_department_list_falls_back_when_modern_documents_do_not_answer(self):
        bodies = (
            "Samar College\nThe academic calendar lists the final examination dates.",
            "Ask your college department about the enrollment requirements.",
            "The College of River Studies held a meeting about the academic calendar.",
            "College of River Studies students attend orientation every Friday.",
            "Academic departments should refer students to their advisers for enrollment.",
            "Year 1 curriculum: field methods, data analysis, and a laboratory class.",
            "Programs Offered 2026",
            "Course Offering\nSee the handbook for the complete list of college departments.",
            "College of River Studies\nRefer to the handbook for all academic departments.",
        )
        old = document(text="College departments: OLD_UNIT and PREVIOUS_UNIT.")
        for body in bodies:
            with self.subTest(body=body):
                current = document("Programs_Offered.docx", body, collection_heading="Programs Offered")
                result = self.select([old, current], "What are the academic departments?",
                                     uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                self.assertFalse(result.restricted)
                self.assertEqual(result.documents, [old, current])

    def test_shs_teacher_rosters_and_qualifications_are_not_college_catalogs(self):
        cases = (
            ("English Department | Teacher: Current Person", {"collection_heading": "Senior High School"}),
            ("English Department | Teacher: Current Person", {"section_heading": "SHS Academic Departments"}),
            ("Senior High School\nEnglish Department | Teacher: Current Person", {}),
            ("Junior High School\nDepartment of River Studies\nBachelor of River Studies", {}),
            ("Samar College\nEnglish Department | Teacher: Current Person\nBachelor of Literature",
             {"section_heading": "Faculty Qualifications"}),
            ("Department of River Studies\nTeacher: Current Person\nBachelor of River Studies",
             {"section_heading": "Academic Departments"}),
            ("Dr. Current Person | Bachelor of River Studies", {"section_heading": "River Studies Department"}),
            ("College of River Studies\nTeacher: Current Person", {"section_heading": "Teacher Roster"}),
        )
        for text, headings in cases:
            with self.subTest(text=text, headings=headings):
                current = document("Programs_Offered.docx", text, **headings)
                old = document()
                result = self.select([old, current], "college department",
                                     uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                self.assertFalse(result.restricted)
                self.assertEqual(result.documents, [old, current])

    def test_department_catalog_does_not_override_chronology(self):
        current = document("Programs_Offered.docx", "College of River Studies\nBachelor of River Studies")
        old = document()
        for stamp in ("2019-06-01", "2027-06-01"):
            with self.subTest(stamp=stamp):
                result = self.select([old, current], "college departments", dates={
                    **DATES, "programs_offered.docx": {"effective_date": stamp},
                }, uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                self.assertFalse(result.restricted)
                self.assertEqual(result.documents, [old, current])
        self.assertFalse(self.select([old, current], "academic departments", uploads={}).restricted)

    def test_historical_department_requests_keep_all_requested_versions(self):
        docs = [document(), document("Programs_Offered.docx", "College of River Studies\nBachelor of River Studies")]
        for query in ("What were the college departments in 2024?", "List academic departments in the 2024 handbook",
                      "Compare the previous and current college departments", "What changed in academic departments?"):
            with self.subTest(query=query):
                result = self.select(docs, query, uploads={docs[1].metadata["source"]: UPLOADS[UPDATE]})
                self.assertFalse(result.restricted)
                self.assertEqual(result.documents, docs)

    def test_explicit_level_and_role_questions_keep_existing_non_list_behavior(self):
        shs = document("Roster.docx", "SHS academic departments: Languages and Mathematics.")
        other = document("Roster.docx", "An unrelated page from the same source.")
        result = self.select([document(), shs, other], "What are the SHS academic departments?",
                             uploads={"Roster.docx": UPLOADS[UPDATE]})
        self.assertEqual(result.documents, [shs, other])
        dean = document("Roster.docx", "Dean of River Studies: Current Person.")
        result = self.select([document(), dean, other], "Who is the college dean of River Studies?",
                             uploads={"Roster.docx": UPLOADS[UPDATE]})
        self.assertEqual(result.documents, [dean, other])

    def test_compound_department_and_fee_aspects_retain_both_current_sources(self):
        fees = document("Fee_Update.docx", "Tuition fees: CURRENT_FEE.")
        queries = ["List college departments and tuition fees", "college departments", "tuition fees"]
        for body in ("College of River Studies", "College of River Studies\nBachelor of River Studies"):
            with self.subTest(body=body):
                catalog = document("Programs_Offered.docx", body, section_heading="College of River Studies",
                                   collection_heading="Programs Offered")
                result = self.select([document(), fees, catalog], queries, uploads={
                    catalog.metadata["source"]: UPLOADS[UPDATE], fees.metadata["source"]: UPLOADS[UPDATE],
                })
                self.assertTrue(result.restricted)
                self.assertEqual(result.documents, [fees, catalog])

    def test_explicit_cas_name_selects_its_old_section_not_similar_current_catalogs(self):
        old = document(text="College of Arts and Sciences (CAS)\nBachelor of Arts in Sociology", page=48,
                       section_heading="College of Arts and Sciences")
        current = [document(source, f"{unit}\n{degree}", collection_heading="Programs Offered")
                   for source, unit, degree in (
                       ("CLAS_Programs.docx", "College of Liberal Arts and Sciences (CLAS)",
                        "Bachelor of Arts in Sociology"),
                       ("Business_Programs.docx", "College of Business", "Bachelor of Business Administration"),
                       ("Management_Programs.docx", "College of Management", "Bachelor of Management"),
                       ("IT_Programs.docx", "College of Information Technology", "Bachelor of Information Technology"),
                   )]
        docs = [*current, document(text="Enrollment requires OLD_FORM.", page=5), old]
        before = deepcopy(docs)
        for query in ("college of arts and science", "Tell me about college of art and science.",
                      "What programs does College of Arts and Sciences offer?",
                      "What programs does cOlLeGe oF ArT aNd ScIeNcEs offer?"):
            with self.subTest(query=query):
                result = self.select(docs, query,
                                     uploads={doc.metadata["source"]: UPLOADS[UPDATE] for doc in current})
                self.assertEqual(result.documents, [old])
                self.assertIs(result.documents[0], old)
                self.assertEqual(result.documents[0].metadata["source"], BASE)
                self.assertEqual(result.documents[0].metadata["page"], 48)
        self.assertEqual(docs, before)

    def test_dean_query_retains_academic_council_name_and_designation(self):
        profile = document(text="The CAS is headed by a College Dean under the Vice-President.",
                           section_heading="College of Arts and Sciences", page=31)
        roster = document(text="Dr. River Example, PhD Vice-President for Academic Affairs / Dean College of Arts and Sciences",
                          section_heading="Academic Council", page=144)
        other = document("Current_Programs.docx", "College of Liberal Arts and Sciences\nBachelor of Arts in Sociology.")
        result = self.select([profile, other, roster], "dean of cas",
                             uploads={other.metadata["source"]: UPLOADS[UPDATE]})
        self.assertEqual(result.documents, [profile, roster])
        self.assertIn("Dr. River Example", result.documents[1].page_content)
        self.assertEqual(result.documents[1].metadata["page"], 144)

    def test_named_unit_matching_does_not_accept_inserted_or_appended_modifiers(self):
        for unit, other in (("College of River Studies", "College of Applied River Studies"),
                            ("College of Valley Studies", "College of Valley Studies and Technology")):
            with self.subTest(unit=unit, other=other):
                old = document(text="Bachelor of Community Studies", section_heading=unit)
                current = document("Programs_Offered.docx", "Bachelor of Community Studies",
                                   section_heading=other, collection_heading="Programs Offered")
                result = self.select([current, old], f"What programs does {unit.lower()} offer?",
                                     uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                self.assertEqual(result.documents, [old])

    def test_newer_exact_cas_coverage_wins_without_an_always_handbook_rule(self):
        unit = "College of Arts and Sciences (CAS)"
        old = document(text=f"{unit}\nBachelor of Arts in OLD_SUBJECT")
        current = document("Programs_Offered.docx", f"{unit}\nBachelor of Arts in CURRENT_SUBJECT")
        other = document("Other_Programs.docx", "College of Liberal Arts and Sciences (CLAS)\n"
                         "Bachelor of Arts in OTHER_SUBJECT", collection_heading="Programs Offered")
        for chronology in ("upload", "effective_date"):
            with self.subTest(chronology=chronology):
                dates = dict(DATES)
                uploads = {}
                for doc in (current, other):
                    source = doc.metadata["source"]
                    if chronology == "upload":
                        uploads[source] = UPLOADS[UPDATE]
                    else:
                        dates[source.lower()] = {"effective_date": "2026-06-01", "date_source": "document"}
                result = self.select([other, old, current], "What programs does College of Arts and Sciences offer?",
                                     dates=dates, uploads=uploads)
                self.assertEqual(result.documents, [current])
                self.assertIs(result.documents[0], current)

    def test_explicit_unit_acronyms_do_not_match_other_units(self):
        for unit, acronym, other, other_acronym in (
            ("College of Arts and Sciences", "CAS", "College of Liberal Arts and Sciences", "CLAS"),
            ("College of River Studies", "CRS", "College of Applied River Studies", "CARS"),
        ):
            old = document(text=f"{unit} ({acronym})\nBachelor of Community Studies")
            current = document("Programs_Offered.docx", f"{other} ({other_acronym})\nBachelor of Community Studies")
            for query in (acronym, acronym.lower(), f"What programs does {acronym.title()} offer?"):
                with self.subTest(query=query, other=other):
                    result = self.select([current, old], query,
                                         uploads={current.metadata["source"]: UPLOADS[UPDATE]})
                    self.assertEqual(result.documents, [old])

    def test_explicit_clas_and_broad_catalog_queries_do_not_force_old_cas_fallback(self):
        old = document(text="College of Arts and Sciences (CAS)\nPrograms offered: BA Sociology.")
        clas = document("Programs_Offered.docx", "College of Liberal Arts and Sciences (CLAS)\n"
                        "Bachelor of Arts in Communication", collection_heading="Programs Offered")
        business = document("Business_Catalog.docx", "College of Business and Management\n"
                            "Bachelor of Business Administration", collection_heading="Programs Offered")
        for query, expected in (("College of Liberal Arts and Sciences", [clas]), ("CLAS", [clas]),
                                ("college department", [clas, business]),
                                ("What programs are offered?", [clas, business]),
                                ("Is BA Sociology offered?", [clas, business])):
            with self.subTest(query=query):
                result = self.select([old, clas, business], query, uploads={
                    clas.metadata["source"]: UPLOADS[UPDATE], business.metadata["source"]: UPLOADS[UPDATE],
                })
                self.assertEqual(result.documents, expected)

    def test_named_unit_compound_queries_keep_other_requested_topics(self):
        named_query = "Who is the dean of College of Arts and Sciences?"
        named = document("College_Staff.docx", "College of Arts and Sciences (CAS)\nDean: Dr. Current Dean.")
        for other_query, text in (("Who is the principal?", "Principal: Ms. Current Head."),
                                  ("What are the tuition fees?", "Tuition fees: CURRENT_FEE.")):
            with self.subTest(other_query=other_query):
                other = document("Campus_Update.docx", text)
                queries = [f"{named_query[:-1]} and {other_query.lower()}", named_query, other_query]
                result = self.select([document(), other, named], queries, uploads={
                    named.metadata["source"]: UPLOADS[UPDATE], other.metadata["source"]: UPLOADS[UPDATE],
                })
                self.assertEqual(result.documents, [other, named])


class SourceDiscoveryTests(unittest.TestCase):
    def test_enrollment_probes_are_bounded_and_distinct_from_offering_requests(self):
        for query in ("how to enroll in bsit", "How do I register for BSIT?", "BSIT admission requirements", "enrolment procedure"):
            with self.subTest(query=query):
                self.assertEqual(enrollment_queries(query), ["college enrollment enrolment procedure admission requirements registration"])
                self.assertEqual(program_catalog_queries(query), [])
        for query in ("What programs are offered?", "Who is the BSIT coordinator?", "My enrollment history", "", None):
            self.assertEqual(enrollment_queries(query), [])
        self.assertTrue(enrollment_queries("How do I enroll in SHS?")[0].startswith("How do I enroll in SHS?"))

    def test_program_probes_preserve_qualifiers_and_do_not_reclassify_other_questions(self):
        for query in ("confirmed current offerings", "current senior high programs",
                      "current offerings for College of River Studies", "programs offered in 2024"):
            with self.subTest(query=query):
                probes = program_catalog_queries(query)
                self.assertEqual(len(probes), 2)
                self.assertTrue(all(probe.startswith(query + " ") for probe in probes))
        for query in ("What are the tuition fees for BSIT?", "Who manages degree programs?",
                      "What is a degree?", "Current subjects and prerequisites", "When are course offerings announced?",
                      "Compare current and 2024 program offerings", None, ""):
            with self.subTest(query=query):
                self.assertEqual(program_catalog_queries(query), [])

    def newer(self, *, dates=None, uploads=None, **kwargs):
        with patch("rag.freshness.get_doc_dates", side_effect=AssertionError("No store reads")), \
                patch("rag.freshness._store", side_effect=AssertionError("No store reads")):
            return newer_source_names(dates=dates, uploads=uploads, today=TODAY, **kwargs)

    def test_raw_upload_spelling_precedes_date_record_filename_and_keys(self):
        uploads = {"s3://bucket/Programs_Offered.DOCX": UPLOADS[UPDATE],
                   r"C:\uploads\COURSE-Offerings.docx": "invalid", "Calendar.docx": UPLOADS[UPDATE]}
        dates = {**DATES, "programs_offered.docx": {"filename": "Programs_Offered.docx", "effective_date": "2026-06-01"},
                 "course-offerings.docx": {"filename": "Course-Offerings.docx", "effective_date": "2025-06-01"},
                 "academic_catalog.pdf": {"filename": "Academic_Catalog.PDF", "effective_date": "2025-08-01"}}
        before = deepcopy((dates, uploads))
        self.assertEqual(self.newer(dates=dates, uploads=uploads),
                         [*uploads, "Academic_Catalog.PDF"])
        self.assertEqual((dates, uploads), before)

    def test_discovery_returns_all_potential_sources_before_query_relevance(self):
        dates = {**DATES, "policy-2025.pdf": {"filename": "Policy-2025.PDF"},
                 "policy-2026.pdf": {"filename": "Policy-2026.PDF"}}
        uploads = {"Unrelated_Calendar.docx": UPLOADS[UPDATE], "Course-Offerings.docx": UPLOADS[UPDATE]}
        self.assertEqual(self.newer(dates=dates, uploads=uploads), [*uploads, "Policy-2025.PDF", "Policy-2026.PDF"])
        self.assertEqual(self.newer(dates=dates), ["Policy-2025.PDF", "Policy-2026.PDF"])

    def test_distinct_raw_upload_keys_are_not_lost_to_normalized_deduplication(self):
        uploads = {"Programs_Offered.DOCX": UPLOADS[UPDATE], "programs_offered.docx": UPLOADS[UPDATE]}
        dates = {"programs_offered.docx": {"filename": "PROGRAMS_OFFERED.docx", "effective_date": "2026-06-01"}}
        self.assertEqual(self.newer(dates=dates, uploads=uploads), list(uploads))

    def test_discovery_and_selection_share_date_and_upload_eligibility(self):
        cases = (
            (None, None, False), (None, "invalid", False), (None, "2023-01-01T00:00:00Z", False),
            (None, "2027-01-01T00:00:00Z", False), (None, UPLOADS[UPDATE], True),
            ("invalid", "invalid", False), ("2019-06-01", UPLOADS[UPDATE], False),
            ("2027-06-01", UPLOADS[UPDATE], False), ("2024-06-01", None, False),
            ("2024-06-01", UPLOADS[UPDATE], True), ("2025-06-01", None, True),
            ("2025-06-01", "invalid", True), ("2025-06-01", "2027-01-01T00:00:00Z", True),
        )
        for effective, uploaded, expected in cases:
            with self.subTest(effective=effective, uploaded=uploaded):
                dates = {**DATES, UPDATE.lower(): {"filename": UPDATE, "effective_date": effective}}
                uploads = {UPDATE: uploaded}
                names = self.newer(dates=dates, uploads=uploads)
                self.assertEqual(names, [UPDATE] if expected else [])
                result = select_documents([document(), document(UPDATE)], "Enrollment requirements",
                                          dates=dates, uploads=uploads, today=TODAY)
                self.assertEqual(result.restricted, expected)

    def test_filename_dates_and_recorded_dates_keep_existing_precedence(self):
        dates = {**DATES, "old-title-2019.pdf": {"filename": "Old-Title-2019.PDF", "effective_date": "2026-06-01"},
                 "misleading-2026.pdf": {"filename": "Misleading-2026.PDF", "effective_date": "2019-06-01"},
                 "dated-2025.pdf": {}, "invalid-2026.pdf": {"effective_date": "not-a-date"}}
        self.assertEqual(self.newer(dates=dates), ["Old-Title-2019.PDF", "dated-2025.pdf", "invalid-2026.pdf"])

    def test_invalid_and_unproven_undated_sources_do_not_get_invented_dates(self):
        dates = {"unknown.docx": {"filename": "Unknown.docx", "updated_at": UPLOADS[UPDATE], "first_seen": UPLOADS[UPDATE]},
                 "invalid.docx": {"filename": "Invalid.docx", "effective_date": "2026-02-30"},
                 "none.docx": None, "number.docx": {"effective_date": 2026}}
        self.assertEqual(self.newer(dates=dates, uploads={"Future.docx": "2027-01-01T00:00:00Z"}), [])
        self.assertEqual(self.newer(), [])

    def test_default_and_renamed_handbooks_are_never_discovery_candidates(self):
        dates = {**DATES, "student-handbook-2026.pdf": {"filename": "Student-Handbook-2026.PDF",
                                                       "effective_date": "2024-06-01"}}
        uploads = {BASE: UPLOADS[UPDATE], r"C:\uploads\STUDENT-Handbook-2024.PDF": UPLOADS[UPDATE],
                   "Student-Handbook-2026.PDF": UPLOADS[UPDATE], UPDATE: "2026-11-13T02:39:29Z"}
        self.assertEqual(self.newer(dates=dates, uploads=uploads), [UPDATE])

    def test_custom_disabled_and_unknown_baselines_match_selection(self):
        dates = {"reference.pdf": {"filename": "Reference.PDF", "effective_date": "2023-06-01"},
                 UPDATE.lower(): {"filename": UPDATE, "effective_date": "2024-01-01"}}
        self.assertEqual(self.newer(dates=dates, fallback_sources=["s3://bucket/REFERENCE.pdf"]), [UPDATE])
        for baseline in ([], ["Undated.pdf"], ["Handbook-2027.pdf"]):
            with self.subTest(baseline=baseline):
                self.assertEqual(self.newer(dates=dates, uploads=UPLOADS, fallback_sources=baseline), [])

    def test_equal_uploads_are_compared_as_utc_instants(self):
        for timestamp, expected in (("2026-11-13T10:39:28+08:00", []),
                                    ("2026-11-13T10:39:29+08:00", [UPDATE]),
                                    ("2026-11-13T02:39:28", [])):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.newer(dates=DATES, uploads={BASE: UPLOADS[UPDATE], UPDATE: timestamp}), expected)

    def test_discovery_does_not_change_dated_only_selection_without_a_fallback_hit(self):
        current = document("Policy-2026.PDF")
        dates = {"policy-2026.pdf": {"filename": "Policy-2026.PDF"}}
        self.assertEqual(self.newer(dates=dates), ["Policy-2026.PDF"])
        result = select_documents([current], "Enrollment requirements", dates=dates, uploads={}, today=TODAY)
        self.assertFalse(result.restricted)
        self.assertEqual(result.documents, [current])

    def test_dated_first_pass_requires_recorded_candidate_and_baseline_provenance(self):
        current = document("Programs_Offered.docx", "College of River Studies\nBachelor of River Studies")
        current_record = {"programs_offered.docx": {"filename": "Programs_Offered.docx", "effective_date": "2026-06-01"}}
        baselines = (DATES, {"student-handbook.pdf": {"filename": "Student-Handbook.PDF", "effective_date": "2024-06-01"}})
        for baseline in baselines:
            with self.subTest(baseline=baseline):
                dates = {**baseline, **current_record}
                self.assertEqual(self.newer(dates=dates), ["Programs_Offered.docx"])
                result = select_documents([current], "college departments", dates=dates, uploads={}, today=TODAY)
                self.assertTrue(result.restricted)
                self.assertEqual(result.documents, [current])
        for dates in (current_record, DATES, {**current_record, BASE.lower(): {"effective_date": "2027-06-01"}}):
            with self.subTest(missing_or_future_baseline=dates):
                result = select_documents([current], "college departments", dates=dates, uploads={}, today=TODAY)
                self.assertFalse(result.restricted)


class DepartmentProbeTests(unittest.TestCase):
    def test_natural_broad_singular_and_plural_list_requests_have_two_generic_probes(self):
        probes = ["college departments programs offered", "college academic units course offerings"]
        for query in ("college department", "academic departments", "What are the academic department?",
                      "What is the college department?", "List all college departments", "Name the academic units",
                      "Which departments does the college have?", "What are the departments at Example College?",
                      "Please show me the college departments", "Tell me all the academic departments",
                      "COLLEGE ACADEMIC DEPARTMENTS", "Current academic departments"):
            with self.subTest(query=query):
                self.assertEqual(department_catalog_queries(query), probes)
                self.assertTrue(all("example" not in probe for probe in probes))

    def test_specific_services_roles_and_non_list_questions_are_not_broadened(self):
        for query in ("Who is the dean of the college department?", "List college department heads",
                      "College academic department faculty", "What is the college IT support department contact?",
                      "What is the college guidance department?", "List academic department services",
                      "What does an academic department do?", "What is an academic department?",
                      "Where is the academic department office?", "How many academic departments are there?",
                      "What are the fees for the college department?", "What is the River Studies department at college?",
                      "Give the academic department phone number", "Academic department admission requirements",
                      "What are the college department responsibilities?", "What is enrollment?", "Programs offered",
                      "What programs does the academic department offer?", "List courses in the college department",
                      "What is my college department?", "Tell me about college department activities",
                      "The college department has a meeting tomorrow", "college IT department", "", None):
            with self.subTest(query=query):
                self.assertEqual(department_catalog_queries(query), [])

    def test_explicit_other_school_levels_are_not_broadened_into_college_lists(self):
        for level in ("SHS", "JHS", "senior high school", "junior high", "elementary", "basic education",
                      "graduate", "postgraduate", "primary", "secondary", "kindergarten"):
            with self.subTest(level=level):
                self.assertEqual(department_catalog_queries(f"List the {level} academic departments"), [])
                self.assertEqual(department_catalog_queries(f"List college and {level} departments"), [])

    def test_catalog_source_hints_preserve_raw_paths_case_and_order(self):
        catalog = [r"C:\uploads\PROGRAMS_Offered.DOCX", "s3://bucket/Course-Offering.pdf", "Program-Catalog.PDF",
                   "Course_Catalogue.docx", "Academic_Catalog-2026.pdf", "Programme_Offerings.docx"]
        sources = [catalog[0], "Student-Handbook.pdf", *catalog[1:], "Curriculum-2026.docx", "Program_Policies.pdf",
                   "General_Course_Outline.docx", "Faculty_Roster.docx", catalog[0], None]
        before = list(sources)
        self.assertEqual(catalog_source_names(sources), catalog)
        self.assertEqual(sources, before)
        self.assertEqual(catalog_source_names(catalog[0]), [catalog[0]])
        self.assertEqual(catalog_source_names([]), [])


class UploadChronologyTests(unittest.TestCase):
    def test_metadata_scan_is_complete_projected_and_read_only(self):
        source = Path("aws/dynamodb.py")
        function = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                        if isinstance(node, ast.FunctionDef) and node.name == "get_file_upload_dates")
        table = Mock()
        table.scan.side_effect = [
            {"Items": [{"filename": UPDATE, "uploaded_at": UPLOADS[UPDATE], "uploaded_by": "PRIVATE_UPLOADER"}],
             "LastEvaluatedKey": {"filename": UPDATE}},
            {"Items": [], "LastEvaluatedKey": {"filename": "empty-page"}},
            {"Items": [{"filename": "Programs.docx", "uploaded_at": "2026-11-13T03:00:00Z"}]},
        ]
        namespace = {"files_table": table}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)  # noqa: S102 - trusted repo AST, fake table
        values = namespace["get_file_upload_dates"]()
        self.assertEqual(values, {UPDATE: UPLOADS[UPDATE], "Programs.docx": "2026-11-13T03:00:00Z"})
        self.assertEqual(table.scan.call_args_list[0].kwargs,
                         {"ProjectionExpression": "filename, uploaded_at", "ConsistentRead": True})
        self.assertEqual(table.scan.call_args_list[2].kwargs["ExclusiveStartKey"], {"filename": "empty-page"})
        table.put_item.assert_not_called()
        table.scan.side_effect = [{"Items": [{"filename": UPDATE, "uploaded_at": UPLOADS[UPDATE]}],
                                   "LastEvaluatedKey": {"filename": UPDATE}}, RuntimeError("unavailable")]
        with self.assertRaises(RuntimeError):
            namespace["get_file_upload_dates"]()


if __name__ == "__main__":
    unittest.main()
