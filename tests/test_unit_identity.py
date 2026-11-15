"""Pure identity and dated-guidance checks; no application or service startup."""

import unittest
from datetime import date
from types import SimpleNamespace

import _bootstrap  # noqa: F401

from rag.source_policy import Selection, unit_selection_guidance
from rag.unit_identity import document_matches_unit, requested_unit, same_unit


def document(name, source="Handbook-2024.pdf", text="Bachelor of Community Studies"):
    return SimpleNamespace(page_content=text, metadata={"source": source, "section_heading": name})


class UnitIdentityTests(unittest.TestCase):
    def test_spelling_number_and_case_are_not_new_units(self):
        self.assertTrue(same_unit("college of art and science", "College of Arts and Sciences"))
        self.assertTrue(same_unit("College of River Study", "College of River Studies"))
        self.assertFalse(same_unit("College of Arts and Sciences", "College of Liberal Arts and Sciences"))
        self.assertFalse(same_unit("CAS", "CLAS"))

    def test_citas_stays_distinct_from_cas_even_when_typed_in_lowercase(self):
        citas = document("College of Information Technology and Allied Sciences")
        cas = document("College of Arts and Sciences")
        for question in ("citas", "Citas", "CITAS", "what is citas", "tell me about citas"):
            with self.subTest(question=question):
                request = requested_unit(question)
                self.assertTrue(request.protected)
                self.assertTrue(document_matches_unit(citas, request))
                self.assertFalse(document_matches_unit(cas, request))
        self.assertTrue(document_matches_unit(cas, requested_unit("cas")))
        self.assertFalse(document_matches_unit(citas, requested_unit("cas")))

    def test_nouns_inside_names_are_not_discarded_as_question_fields(self):
        for name, other in (
            ("College of Applied Information Sciences", "College of Applied"),
            ("College of Social Philosophy", "College of Social"),
            ("Office of Graduate Programs", "Office of Graduate"),
        ):
            with self.subTest(name=name):
                request = requested_unit("Tell me about " + name)
                self.assertEqual(request.name, name)
                self.assertTrue(document_matches_unit(document(name), request))
                self.assertFalse(document_matches_unit(document(other), request))

    def test_only_independent_single_unit_requests_are_bound(self):
        for question in ("college department", "which college units exist?", "and for BSIT", "and for art",
                         "Who is dean of College of Arts and Sciences and who is the principal?",
                         "College of Arts and Sciences and College of Education"):
            with self.subTest(question=question):
                self.assertIsNone(requested_unit(question))
        request = requested_unit("Who was dean of College of Arts and Sciences in 2024?")
        self.assertEqual(request.name, "College of Arts and Sciences")
        self.assertIn("2024", request.remainder)

    def test_incidental_mentions_do_not_establish_the_unit_as_subject(self):
        request = requested_unit("College of Arts and Sciences")
        doc = document("College of Liberal Arts and Sciences",
                       text="Our students visited the College of Arts and Sciences during an event.")
        self.assertFalse(document_matches_unit(doc, request))

    def test_hierarchy_delimiters_do_not_become_name_words(self):
        request = requested_unit("College of Arts and Sciences")
        self.assertTrue(document_matches_unit(document("Programs Offered > College of Arts and Sciences > Programs"), request))

    def test_named_dean_roster_rows_are_not_discarded(self):
        request = requested_unit("dean of cas")
        for row in (
            "Dean, College of Arts and Sciences",
            "Dean: College of Arts and Sciences",
            "OIC-Dean, College of Arts and Sciences",
            "Acting Dean of College of Arts and Sciences",
            "Dean, CAS",
        ):
            with self.subTest(row=row):
                doc = document("Academic Council", text="Dr. River Example, PhD\n" + row)
                before = doc.page_content
                self.assertTrue(document_matches_unit(doc, request))
                self.assertEqual(doc.page_content, before)

    def test_roster_table_cells_preserve_the_unit_without_crossing_names(self):
        doc = document("Academic Council", text=(
            "| Dr. River Example | Dean, College of Arts and Sciences |\n"
            "| Dr. Valley Example | Dean, College of Liberal Arts and Sciences |"
        ))
        self.assertTrue(document_matches_unit(doc, requested_unit("dean of cas")))
        other = document("Academic Council", text="Dr. Valley Example\nDean, College of Liberal Arts and Sciences")
        self.assertFalse(document_matches_unit(other, requested_unit("dean of cas")))

    def test_flattened_named_roster_with_multiple_roles_keeps_exact_affiliation(self):
        text = (
            "AVERY EXAMPLE, PhD Vice-President for Academic Affairs / Dean College of Arts and Sciences\n"
            "JORDAN EXAMPLE, PhD Dean, College of Liberal Arts and Sciences Member"
        )
        doc = document("Academic Council", text=text)
        self.assertTrue(document_matches_unit(doc, requested_unit("dean of cas")))
        self.assertEqual(doc.page_content, text)
        other = document("Academic Council", text=text.splitlines()[1])
        self.assertFalse(document_matches_unit(other, requested_unit("dean of cas")))
        incidental = document("Event Report", text=text.splitlines()[0])
        self.assertFalse(document_matches_unit(incidental, requested_unit("dean of cas")))

    def test_fallback_guidance_requires_a_visible_edition_year(self):
        doc = document("College of Arts and Sciences")
        choice = Selection([doc], False, requested_unit="College of Arts and Sciences")
        text = unit_selection_guidance(choice, [doc], today=date(2026, 11, 15))
        self.assertIn("2024", text)
        self.assertIn("Begin the visible answer", text)
        self.assertIn("does NOT count", text)
        self.assertIn("not an invented policy effective date", text)

    def test_current_undated_and_empty_evidence_do_not_invent_a_year(self):
        dated = document("College of River Studies")
        undated = document("College of River Studies", source="Current_Catalog.docx")
        for choice, docs in (
            (Selection([dated], True, requested_unit="College of River Studies"), [dated]),
            (Selection([undated], False, requested_unit="College of River Studies"), [undated]),
            (Selection([], False, requested_unit="College of River Studies"), []),
        ):
            with self.subTest(docs=docs):
                self.assertNotIn("<dated_unit_evidence>", unit_selection_guidance(choice, docs))
        self.assertEqual(unit_selection_guidance(Selection([dated], False), [dated]), "")


if __name__ == "__main__":
    unittest.main()
