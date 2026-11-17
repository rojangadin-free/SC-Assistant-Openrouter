"""Pure identity and dated-guidance checks; no application or service startup."""

import unittest
from datetime import date
from types import SimpleNamespace

import _bootstrap  # noqa: F401

from rag.source_policy import Selection, unit_selection_guidance
from rag.spelling import correct_query, is_known_acronym
from rag.unit_identity import (
    document_matches_unit, program_affiliations, program_dean_subject, program_overview_subject,
    requested_unit, same_unit, unit_query_variants,
)


def document(name, source="Handbook-2024.pdf", text="Bachelor of Community Studies"):
    return SimpleNamespace(page_content=text, metadata={"source": source, "section_heading": name})


class UnitIdentityTests(unittest.TestCase):
    def test_bare_program_overviews_do_not_capture_questions_or_ordinary_topics(self):
        for question in ("BA sociology", "BMMA", "BSIT", "BSBA", "MAED", "Bachelor of Arts in Sociology"):
            with self.subTest(question=question):
                self.assertEqual(program_overview_subject(question), question)
        for question in ("dean of BA sociology", "CLAS", "admission", "banana", "book", "my grades", "how to enroll",
                         "BA Sociology fees", "What is Bachelor of Arts in Sociology?", ""):
            with self.subTest(question=question):
                self.assertIsNone(program_overview_subject(question))

    def test_clas_accepts_the_documented_short_roster_label_but_not_cas(self):
        text = ("[SECTION: PREFECT OF DISCIPLINE]\nColleges & Academic Units\n"
                "\u2022 MELDRITO B. VALLES, Ph.D. \u2014 Dean, College of Liberal Arts\n"
                "\u25aa Program Head\n\u25e6 Faculty")
        roster = document("PREFECT OF DISCIPLINE", source="Samar_Colleges_Inc_Organizational_Structure_v2_2026.docx", text=text)
        for query in ("dean of CLAS", "dean of clas", "dean of College of Liberal Arts and Sciences"):
            with self.subTest(query=query):
                self.assertTrue(document_matches_unit(roster, requested_unit(query)))
        self.assertTrue(same_unit("College of Liberal Arts", "College of Liberal Arts and Sciences"))
        self.assertFalse(document_matches_unit(roster, requested_unit("dean of CAS")))
        self.assertFalse(same_unit("College of Arts and Sciences", "College of Liberal Arts"))
        self.assertFalse(same_unit("College of Liberal Arts and Design", "College of Liberal Arts and Sciences"))
        self.assertEqual(roster.page_content, text)
        self.assertEqual(unit_query_variants("dean of clas"), ["dean of College of Liberal Arts and Sciences"])
        for unit in ("CLAS", "College of Liberal Arts and Sciences"):
            guidance = unit_selection_guidance(Selection([roster], True, requested_unit=unit), [roster])
            self.assertIn("<established_unit_alias>", guidance)
            self.assertIn("not a guessed department or a name conflict", guidance)
        self.assertNotIn("<established_unit_alias>", unit_selection_guidance(
            Selection([roster], True, requested_unit="College of Education"), [roster]))
        self.assertNotIn("<established_unit_alias>", unit_selection_guidance(
            Selection([], True, requested_unit="CLAS"), []))

    def test_program_dean_targets_are_not_department_aliases(self):
        for subject in ("bmma", "BSIT", "BEED", "Bachelor of Multimedia Arts"):
            self.assertEqual(program_dean_subject(f"who is the dean of {subject}?"), subject)
        for question in ("who is dean of CITAS?", "who is dean of COED?", "who is dean of College of Education?",
                         "who is the president?", "deans of BMMA and BSIT"):
            self.assertIsNone(program_dean_subject(question))
        self.assertFalse(same_unit("BMMA", "College of Information Technology and Allied Sciences"))

    def test_program_dean_question_variants_keep_the_program_label(self):
        for question, subject in (
            ("dean of BMMA.", "BMMA"),
            ("who is the dean of Bachelor of Multimedia Arts (BMMA)?", "Bachelor of Multimedia Arts (BMMA)"),
            ("dean of the BMMA program?", "BMMA"),
            ("dean of BSIT at Samar College?", "BSIT"),
            ("dean of BA Sociology?", "BA Sociology"),
        ):
            with self.subTest(question=question):
                self.assertEqual(program_dean_subject(question), subject)

    def test_live_docx_catalog_rows_use_documented_acronyms_from_other_chunks(self):
        parent = "College of Information Technology and Allied Sciences"
        catalog = document("ACADEMIC PROGRAMS", source="Programs_Offered.docx", text=(
            "[TABLE context: ACADEMIC PROGRAMS | Programs Offered | Undergraduate]\n"
            f"| {parent} |\n| --- |\n"
            "| Bachelor of Science in Information Technology CMO No. 25, s. 2015 |\n"
            "| Bachelor of Multimedia Arts CMO No. 15, s. 2017 |"
        ))
        continuation = document("ACADEMIC PROGRAMS / Bachelor of Multimedia Arts (BMMA) / First Year / Curriculum subjects",
                                source="Curriculum.docx", text="| MM 101 | Introduction to Multimedia Arts |")
        welcome = document("WELCOME MESSAGE", source="Curriculum.docx", text=(
            f"Welcome to the {parent}!\n"
            "Through our Bachelor of Science in Information Technology (BSIT) and "
            "Bachelor of Multimedia Arts (BMMA) programs, we encourage collaboration."
        ))
        self.assertEqual(program_affiliations("BMMA", [catalog]), {})
        self.assertEqual(program_affiliations("BMMA", [continuation, welcome]), {})
        for subject in ("BMMA", "BSIT", "Bachelor of Multimedia Arts", "Bachelor of Multimedia Arts (BMMA)"):
            with self.subTest(subject=subject):
                self.assertEqual(program_affiliations(subject, [catalog, continuation, welcome]), {parent: [catalog]})

    def test_flat_program_aliases_match_current_major_rows_and_graduate_school(self):
        aliases = document("Degree Programs", text=(
            "MASTER OF ARTS IN EDUCATION (MAED) With specialization in: Reading "
            "BACHELOR OF SECONDARY EDUCATION (BSED) With Specialization in: English "
            "BACHELOR OF ELEMENTARY EDUCATION (BEED) with specialization in: Generalist Curriculum "
            "BACHELOR OF SCIENCE IN BUSINESS ADMINISTRATION (BSBA) Major in: Marketing Management "
            "BACHELOR OF ARTS IN SOCIOLOGY (BA Sociology)"
        ))
        catalog = document("Programs Offered", source="Current_Programs.docx", text=(
            "| College of Education |\n| --- |\n"
            "| Bachelor of Elementary Education CMO No. 74, s. 2017 |\n"
            "| Bachelor of Secondary Education Major in English CMO No. 75, s. 2017 |\n"
            "| College of Business and Management |\n"
            "| Bachelor of Science in Business Administration Major in Marketing Management CMO No. 17 |\n"
            "| College of Liberal Arts and Sciences |\n| Bachelor of Arts in Sociology CMO No. 40 |\n"
            "| Graduate School |\n| Master of Arts in Education Major in Reading |"
        ))
        for subject, parent in (("BSED", "College of Education"), ("BEED", "College of Education"),
                                ("BSBA", "College of Business and Management"),
                                ("BA Sociology", "College of Liberal Arts and Sciences"), ("MAED", "Graduate School")):
            with self.subTest(subject=subject):
                self.assertEqual(program_affiliations(subject, [catalog, aliases]), {parent: [catalog]})
        self.assertEqual(requested_unit("dean of Graduate School").name, "Graduate School")
        self.assertTrue(document_matches_unit(document("Graduate School"), requested_unit("dean of Graduate School")))
        self.assertFalse(same_unit("Graduate School", "College of Graduate Studies"))
        self.assertEqual(requested_unit("dean of College of Graduate School").name, "College of Graduate School")
        self.assertFalse(document_matches_unit(document("College of Graduate School"), requested_unit("dean of Graduate School")))

    def test_program_alias_proof_does_not_reintroduce_an_old_parent_or_mutate_documents(self):
        old = document("College of Media Studies", text="Bachelor of Multimedia Arts (BMMA)")
        catalog = document("Programs Offered", source="Current_Programs.docx", text=(
            "College of Information Technology and Allied Sciences\nBachelor of Multimedia Arts CMO No. 15"
        ))
        mapping = program_affiliations("BMMA", [catalog], alias_documents=[old], include_alias_evidence=True)
        proofs = mapping["College of Information Technology and Allied Sciences"]
        self.assertIs(proofs[0], catalog)
        self.assertEqual(proofs[1].page_content, "Bachelor of Multimedia Arts (BMMA)")
        self.assertEqual(proofs[1].metadata["section_heading"], "Program acronym definition")
        self.assertEqual(proofs[1].metadata["source"], old.metadata["source"])
        self.assertNotIn("College of Media Studies", str(proofs[1]))
        self.assertEqual(old.metadata["section_heading"], "College of Media Studies")

    def test_degree_major_qualifiers_and_biography_aliases_are_not_guessed(self):
        catalog = document("Programs Offered", text=(
            "College of Business Studies\nBachelor of Science in Business Administration Major in Marketing Management CMO No. 17\n"
            "College of Financial Studies\nBachelor of Science in Business Administration Major in Financial Management CMO No. 17"
        ))
        subject = "Bachelor of Science in Business Administration Major in Financial Management"
        self.assertEqual(program_affiliations(subject, [catalog]), {"College of Financial Studies": [catalog]})
        biography = document("Faculty Qualifications", text="Bachelor of Multimedia Arts (BMMA)")
        plain = document("Programs Offered", text="College of Media Studies\nBachelor of Multimedia Arts CMO No. 15")
        self.assertEqual(program_affiliations("BMMA", [plain, biography]), {})

    def test_actual_bulleted_roster_uses_its_inline_heading_not_stale_chunk_metadata(self):
        roster = document("PREFECT OF DISCIPLINE", source="Organizational_Structure.docx", text=(
            "[SECTION: PREFECT OF DISCIPLINE]\n\u2022 Prefect of Discipline\nColleges & Academic Units\n"
            "\u2022 Synthetic Official - Dean, College of Information Technology & Allied Sciences\n"
            "\u25aa Program Head, BMMA\n\u2022 Other Official - Dean, College of Education\n"
            "\u2022 Graduate Official - Dean, Graduate School"
        ))
        for query in ("dean of CITAS", "dean of CoEd", "dean of Graduate School"):
            with self.subTest(query=query):
                self.assertTrue(document_matches_unit(roster, requested_unit(query)))
        self.assertIn("Synthetic Official - Dean", roster.page_content)
        report = document("Event Report", text="Synthetic Official - Dean, College of Education")
        self.assertFalse(document_matches_unit(report, requested_unit("dean of CoEd")))

    def test_program_affiliations_follow_each_named_catalog_parent(self):
        doc = document("COURSE OFFERING", text="College of Education (CoEd)\n"
                       "o Bachelor of Secondary Education (BSED)\no Bachelor of Elementary Education (BEED)\n"
                       "College of Information Technology and Allied Sciences (CITAS)\n"
                       "o Bachelor of Science in Information Technology (BSIT)\no Bachelor of Multimedia Arts (BMMA)")
        for program in ("BMMA", "bsit", "Bachelor of Multimedia Arts"):
            self.assertEqual(program_affiliations(program, [doc]), {"College of Information Technology and Allied Sciences": [doc]})
        for program in ("BSED", "BEED"):
            self.assertEqual(program_affiliations(program, [doc]), {"College of Education": [doc]})
        self.assertEqual(program_affiliations("UNKNOWN", [doc]), {})

    def test_program_affiliations_require_structural_parent_not_biography_or_cooccurrence(self):
        biography = document("Faculty Qualifications", text="College of Education\nBachelor of Multimedia Arts (BMMA)")
        unowned = document("Programs Offered", text="Bachelor of Multimedia Arts (BMMA)")
        other_parent = document("Programs Offered", text="College of Education\nBachelor of Elementary Education (BEED)\n"
                                "College of Information Technology and Allied Sciences\nBachelor of Multimedia Arts (BMMA)")
        self.assertEqual(program_affiliations("BMMA", [biography, unowned]), {})
        self.assertEqual(list(program_affiliations("BMMA", [other_parent])), ["College of Information Technology and Allied Sciences"])

    def test_wrapped_and_metadata_program_rows_preserve_explicit_affiliation(self):
        wrapped = document("Programs Offered", text="College of Information Technology and Allied Sciences\nBachelor of Multimedia\nArts (BMMA)")
        curriculum = SimpleNamespace(page_content="Year 1 curriculum and subject table", metadata={
            "collection_heading": "College of Information Technology and Allied Sciences", "section_heading": "Bachelor of Multimedia Arts (BMMA)",
        })
        for doc in (wrapped, curriculum):
            self.assertEqual(program_affiliations("BMMA", [doc]), {"College of Information Technology and Allied Sciences": [doc]})

    def test_multiple_program_parents_remain_ambiguous(self):
        first = document("College of Media Studies", text="Bachelor of Multimedia Arts (BMMA)")
        second = document("College of Media Studies and Design", text="Bachelor of Multimedia Arts (BMMA)")
        self.assertEqual(program_affiliations("BMMA", [first, second]), {
            "College of Media Studies": [first], "College of Media Studies and Design": [second],
        })

    def test_coed_is_exactly_college_of_education_not_other_colleges(self):
        education = document("College of Education", text="Example Dean\nDean, College of Education")
        for code in ("coed", "CoEd", "COED"):
            with self.subTest(code=code):
                request = requested_unit(f"who is the dean of {code}?")
                self.assertTrue(document_matches_unit(education, request))
                self.assertTrue(same_unit(code, "College of Education"))
                for other in ("College of Engineering", "College of Educational Studies", "College of Education and Development",
                              "College of Information Technology and Allied Sciences"):
                    self.assertFalse(document_matches_unit(document(other), request))
                    self.assertFalse(same_unit(code, other))

    def test_coed_roster_abbreviation_matches_full_name_without_changing_the_source(self):
        for row in ("Dean, COED", "Dean of CoEd", "Acting Dean, COED"):
            doc = document("Academic Council", text="Example Dean, PhD\n" + row)
            self.assertTrue(document_matches_unit(doc, requested_unit("College of Education")))
            self.assertEqual(doc.page_content, "Example Dean, PhD\n" + row)
        incidental = document("Event Report", text="The coed classrooms held an activity.")
        self.assertFalse(document_matches_unit(incidental, requested_unit("coed")))

    def test_deans_update_name_dash_college_rows_establish_exact_identity(self):
        for separator in ("-", "\u2013", "\u2014"):
            text = f"Synthetic Education Dean {separator} College of Education\nSynthetic IT Dean {separator} College of Information Technology & Allied Sciences"
            doc = document("DEANS UPDATE", text=text)
            self.assertTrue(document_matches_unit(doc, requested_unit("dean of coed")))
            self.assertTrue(document_matches_unit(doc, requested_unit("dean of citas")))
            self.assertEqual(doc.page_content, text)
            unrelated = document("Event Report", text=text)
            self.assertFalse(document_matches_unit(unrelated, requested_unit("dean of coed")))

    def test_coed_query_variants_are_additive_and_do_not_expand_adjectives(self):
        question = "who are the dean of coed"
        self.assertEqual(unit_query_variants(question), ["who are the dean of College of Education"])
        self.assertEqual(question, "who are the dean of coed")
        self.assertEqual(unit_query_variants("coed classrooms and activities"), [])
        self.assertTrue(is_known_acronym("coed"))
        self.assertEqual(correct_query("who are the dean of citas and coed also who is the president of samar college"), "")

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
                          "who are the dean of citas and coed also who is the president of samar college",
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
