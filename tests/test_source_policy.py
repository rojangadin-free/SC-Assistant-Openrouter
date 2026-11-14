"""Offline evidence-boundary tests; no models, cloud clients, or runtime stores."""
import ast
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401

from rag.source_policy import select_documents

BASE = "Samar-College-2024.pdf"
UPDATE = "Samar_Colleges_Inc_Admission_Policy.docx"
TODAY = date(2026, 11, 14)
DATES = {BASE.lower(): {"effective_date": "2024-06-01", "date_source": "document"}}
UPLOADS = {UPDATE: "2026-11-13T02:39:28+00:00"}


def document(source=BASE, text="Enrollment requires OLD_CHECKLIST and OLD_FEE.", **metadata):
    return SimpleNamespace(page_content=text, metadata={"source": source, "page": 1, **metadata})


class SourcePolicyTests(unittest.TestCase):
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

    def test_catalog_cannot_replace_uncovered_fee_guidance(self):
        current = document("Programs_Offered.docx", "Course Offering\nBSIT and BSBA are listed.", section="Course Offering")
        old = document(text="BSIT tuition fee is OLD_FEE.")
        result = self.select([old, current], "What are the tuition fees for BSIT?",
                             uploads={current.metadata["source"]: UPLOADS[UPDATE]})
        self.assertFalse(result.restricted)
        self.assertIn(old, result.documents)

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
                self.assertEqual(result.documents, [old, new])

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
