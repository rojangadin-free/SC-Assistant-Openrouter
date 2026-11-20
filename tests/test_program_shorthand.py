"""Offline degree-code shorthand parsing, repair, and dean retrieval checks."""
import os
import unittest
from unittest.mock import Mock, patch

import test_newer_retrieval as fixtures


class ProgramShorthandTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.NewerRetrievalTests(methodName="runTest")
        self.h.assert_offline = self.assert_offline
        self.addCleanup(self.h.doCleanups)
        self.h.setUp()

    def assert_offline(self):
        self.assertEqual(self.h.denied_imports, [])
        self.assertEqual(self.h.env_reads, [])
        for connection in self.h.sockets:
            connection.assert_not_called()

    def test_only_unambiguous_known_degree_code_suffixes_supply_hints(self):
        spelling = self.h.spelling
        self.assertEqual(spelling.program_code_candidates("crim"), ["bscrim"])
        self.assertEqual(spelling.program_code_candidates("CRIM"), ["bscrim"])
        self.assertEqual(spelling.program_code_candidates("mma"), ["bmma"])
        for subject in ("crime", "unknown", "crim aviation", "it", "citas", "clas", "bscrim", "crim101"):
            self.assertEqual(spelling.program_code_candidates(subject), [])
        with patch.object(spelling, "_SEED_ACRONYMS", ("bscrim", "bacrim")):
            self.assertEqual(spelling.program_code_candidates("crim"), [])

    def test_spelling_preserves_dean_shorthand_but_still_repairs_grammar(self):
        spelling = self.h.spelling
        vocabulary = spelling._Vocabulary({**spelling._vocabulary().freq, "crime": 1000})
        with patch.object(spelling, "_vocabulary", return_value=vocabulary):
            self.assertEqual(spelling.correct_query("dean of crim"), "")
            self.assertEqual(spelling.correct_query("dean of crim who si?"), "dean of crim who is?")
            self.assertEqual(spelling.correct_query("pls dean of crim who si?"), "pls dean of crim who is?")
            self.assertEqual(spelling.correct_query("crim dean pls?"), "")
            for question in ("crim", "crim subjects", "Crim subjects", "CRIM subjects",
                             "the crim rate", "the crime rate", "CRIM-101", "bscrim"):
                with self.subTest(question=question):
                    self.assertEqual(spelling.correct_query(question), "")
                    self.assertEqual(spelling.misspellings(question), [])
                    self.assertEqual(spelling.spelling_variants(question), [])
            self.assertIsNone(spelling.suggest("crim"))
            self.assertNotIn("crim", spelling.spelling_candidates("crim subjects"))

    def test_catalog_must_prove_program_and_parent_not_a_seed_hint_alone(self):
        from rag.unit_identity import program_affiliations, program_dean_subject

        parent = "College of Criminal Justice Education"
        catalog = fixtures.document("Programs_2026.docx", parent + "\nBachelor of Science in Criminology", "catalog", section_heading="Programs Offered")
        alias = fixtures.document(fixtures.BASE, "Bachelor of Science in Criminology (BSCRIM)", "alias")
        for question in ("dean of crim", "dean of crim who is?", "crim dean pls?", "who is CRIM dean?"):
            self.assertEqual(program_dean_subject(question).lower(), "crim")
        self.assertEqual(program_affiliations("crim", [catalog]), {})
        self.assertEqual(program_affiliations("crim", [alias]), {})
        self.assertEqual(program_affiliations("crim", [catalog, alias]), {parent: [catalog]})
        wrapped = fixtures.document("Programs_2026.docx", parent + "\nBachelor of Science in\nCriminology (BSCRIM)", "wrapped")
        self.assertEqual(program_affiliations("crim", [catalog, alias, wrapped]), {parent: [catalog, wrapped]})
        for subject in ("crime", "crim aviation", "unknown"):
            self.assertEqual(program_affiliations(subject, [catalog, alias]), {})
        conflicting = fixtures.document("Programs_2026.docx", parent + "\nBachelor of Arts in Criminal Studies (BSCRIM)", "conflict")
        self.assertEqual(program_affiliations("crim", [catalog, alias, conflicting]), {})

    def test_shorthand_reaches_named_roster_across_source_modes_and_failed_rewrites(self):
        h = self.h
        parent = "College of Criminal Justice Education"
        catalog = fixtures.document("Programs_2026.docx", parent + "\nBachelor of Science in Criminology", "catalog", section_heading="Programs Offered")
        alias = fixtures.document(fixtures.BASE, "Bachelor of Science in Criminology (BSCRIM)", "alias", page=44)
        roster = fixtures.document("Organizational_Structure_2026.docx", "Alex Example, MSCJ\nOIC Dean, " + parent, "roster")
        unrelated = fixtures.document("Programs_2026.docx", "College of Engineering\nBachelor of Science in Engineering", "other")
        for question in ("dean of crim", "dean of crim who si?", "crim dean pls?"):
            for mode in ("true", "false"):
                for failed in (True, False):
                    with self.subTest(question=question, mode=mode, failed=failed), patch.dict(os.environ, {
                        "RAG_SOURCE_FILTER_ENABLED": mode,
                    }):
                        hybrid = h.install([catalog, roster, unrelated], baseline=[])
                        hybrid.ensemble.invoke.side_effect = lambda query: [catalog, roster, alias] if "bscrim" in query.lower() else [catalog, roster, unrelated]
                        h.ns["query_optimizer_model"].invoke = Mock(
                            side_effect=RuntimeError("optimizer unavailable") if failed else None,
                            return_value=fixtures.SimpleNamespace(content="dean of crime department"),
                        )
                        result = h.chat(question)
                        self.assertIn("roster", result["citations"], h.output.getvalue())
                        self.assertIn("catalog", result["citations"])
                        prompt = result["messages_to_llm"][0].content
                        self.assertIn("OIC Dean, " + parent, prompt)
                        self.assertIn("<documented_program_affiliation>", prompt)
                        self.assertNotIn("College of Engineering", prompt)

    def test_crim_subjects_retrieves_program_curriculum_not_degree_offerings(self):
        h = self.h
        source = "samar_colleges_ccje_program_2026.docx"
        curriculum = [
            fixtures.document(source, "Course Code | Descriptive Title | Units\nGE 1 | Understanding the Self | 3",
                              "first-semester", section_heading="Bachelor of Science in Criminology / First Year / First Semester"),
            fixtures.document(source, "Course Code | Descriptive Title | Units\nPE 2 | Fitness Exercises | 2",
                              "continuation", section_heading="Bachelor of Science in Criminology / First Year / First Semester"),
            fixtures.document(source, "Course Code | Descriptive Title | Units\nGE 3 | Readings in Philippine History | 3",
                              "second-semester", section_heading="Bachelor of Science in Criminology / First Year / Second Semester"),
        ]
        catalog = fixtures.document("Programs_2026.docx", "College of Criminal Justice Education\n"
                                    "Bachelor of Science in Criminology (BSCRIM)", "catalog", section_heading="Programs Offered")
        unrelated = fixtures.document(source, "Course Code | Descriptive Title | Units\nIT 101 | Unrelated Computing | 3",
                                      "unrelated", section_heading="Bachelor of Science in Information Technology / First Year / First Semester")
        old = fixtures.document(fixtures.BASE, "Course Code | Descriptive Title | Units\nCRIM 1 | OLD_CURRICULUM | 3",
                                "old", section_heading="BSCRIM Curriculum")
        for question in ("crim subjects", "BSCRIM subjects", "Criminology curriculum"):
            for mode in ("true", "false"):
                for hint in (None, "BSIT first year first semester subjects"):
                    with self.subTest(question=question, mode=mode, hint=hint), patch.dict(os.environ, {
                        "RAG_SOURCE_FILTER_ENABLED": mode,
                    }), patch.dict(h.ns, {"FINAL_TOP_K": 3}):
                        hybrid = h.install([catalog, unrelated, *curriculum], baseline=[old])
                        hybrid.filtered = lambda query, metadata_filter: (
                            hybrid.matching(query, metadata_filter) if metadata_filter == {"source": {"$eq": source}}
                            else [catalog, unrelated]
                        )
                        h.ns["query_optimizer_model"].invoke = Mock(
                            side_effect=RuntimeError("optimizer unavailable") if hint is None else None,
                            return_value=fixtures.SimpleNamespace(content=hint),
                        )
                        result = h.chat(question, history=[{"role": "assistant", "content": "STALE_PROGRAM_ANSWER"}])
                        self.assertEqual(set(result["citations"]), {"first-semester", "continuation", "second-semester"},
                                         h.output.getvalue())
                        prompt = result["messages_to_llm"][0].content
                        for passage in curriculum:
                            self.assertIn(passage.page_content, prompt)
                        for unsupported in ("Unrelated Computing", "OLD_CURRICULUM", "STALE_PROGRAM_ANSWER"):
                            self.assertNotIn(unsupported, prompt)
                        self.assertIn("<curriculum_subject_request>", prompt)
                        self.assertNotIn("<standalone_topic_overview>", prompt)
                        self.assertNotIn("<program_list_request>", prompt)
                        self.assertEqual(result["messages_to_llm"][-1].content, question)
                        self.assertTrue(any(metadata_filter == {"source": {"$eq": source}}
                                            and "BSCRIM" in query for _, query, metadata_filter in hybrid.calls))

    def test_legacy_category_tables_answer_crim_subjects_without_inventing_semesters(self):
        h = self.h
        source = "samar_colleges_ccje_program_2026.docx"
        owner = fixtures.document(source, "College of Criminal Justice Education\nCurriculum\n"
                                  "Bachelor of Science in Criminology", "owner")
        categories = ("General Education (24 units)", "General Education Electives (12 units)",
                      "Additional EC Courses (7 units)", "Physical Education (8 units)",
                      "NSTP (6 units)", "Professional Courses (57 units)")
        tables = [fixtures.document(source, "COURSE CODE | COURSE TITLE | UNITS | PRE-REQUISITE\n"
                                    f"{'AdGE' if index == 2 else f'CoCJE GE {index + 111}'} | Category Subject {index} | 3 units | None",
                                    f"category-{index}", section_heading=category + " / Curriculum subjects")
                  for index, category in enumerate(categories)]
        catalog = fixtures.document("Programs_2026.docx", "College of Criminal Justice Education\n"
                                    "Bachelor of Science in Criminology", "catalog", section_heading="Programs Offered")
        hybrid = h.install([catalog, owner, *tables], baseline=[])
        hybrid.filtered = lambda query, metadata_filter: (
            hybrid.matching(query, metadata_filter) if metadata_filter == {"source": {"$eq": source}} else [catalog]
        )
        h.ns["query_optimizer_model"].invoke.side_effect = RuntimeError("optimizer unavailable")
        with patch.object(h.reranker, "_load_model", return_value=None):
            result = h.chat("crim subjects")
        self.assertEqual(set(result["citations"]), {f"category-{index}" for index in range(6)}, h.output.getvalue())
        prompt = result["messages_to_llm"][0].content
        self.assertIn("<curriculum_subject_request>", prompt)
        self.assertIn("never invent a term", prompt)
        self.assertNotIn("<standalone_topic_overview>", prompt)
        for index, category in enumerate(categories):
            self.assertIn(category, prompt)
            self.assertIn(f"Category Subject {index}", prompt)
        self.assertTrue(all("collection_heading" not in table.metadata for table in tables))

    def test_it_subjects_keeps_every_semester_despite_search_and_final_context_limits(self):
        h = self.h
        source = "College_of_Information_Technology_and_Allied_Sciences_Curriculum_2026.docx"
        degree = "Bachelor of Science in Information Technology"
        semesters = [(year, semester) for year in ("First", "Second", "Third", "Fourth")
                     for semester in ("First", "Second")]
        tables = [fixtures.document(source, "COURSE CODE | COURSE TITLE | UNITS\n"
                                    f"GE {index + 101} | Semester Subject {index} | 3",
                                    f"bsit-{index}", section_heading=f"{degree} / {year} Year / {semester} Semester")
                  for index, (year, semester) in enumerate(semesters)]
        other = fixtures.document(source, "COURSE CODE | COURSE TITLE | UNITS\n"
                                  "MMA 101 | WRONG_DEGREE_SUBJECT | 3", "bmma",
                                  section_heading="Bachelor of Multimedia Arts / First Year / First Semester")
        old = fixtures.document(fixtures.BASE, "COURSE CODE | COURSE TITLE | UNITS\n"
                                "IT 100 | OBSOLETE_SUBJECT | 3", "old-bsit", section_heading=degree + " Curriculum")
        for question in ("it subjects.", "BSIT subjects", "Information Technology curriculum"):
            for hint in (None, "BSIT second year second semester subjects", "BSCRIM subjects"):
                with self.subTest(question=question, hint=hint), patch.dict(h.ns, {"FINAL_TOP_K": 3}):
                    hybrid = h.install([other, *tables], baseline=[old])

                    def filtered(query, metadata_filter):
                        scoped = hybrid.matching(query, metadata_filter)
                        # The broad search misses year 1/semester 1 and both year 3 semesters.
                        for (year, semester), table in zip(semesters, tables):
                            if (f"{year} Year".lower() in query.lower()
                                    and f"{semester} Semester".lower() in query.lower()
                                    and "BSIT" in query and metadata_filter == {"source": {"$eq": source}}):
                                return [table]
                        return [doc for doc in scoped if doc not in (tables[0], tables[4], tables[5])]

                    hybrid.filtered = filtered
                    h.ns["query_optimizer_model"].invoke = Mock(
                        side_effect=RuntimeError("offline") if hint is None else None,
                        return_value=fixtures.SimpleNamespace(content=hint),
                    )
                    result = h.chat(question, history=[{"role": "user", "content": "Criminology"},
                                                      {"role": "assistant", "content": "STALE_CURRICULUM"}])
                    self.assertEqual(set(result["citations"]), {f"bsit-{index}" for index in range(8)}, h.output.getvalue())
                    prompt = result["messages_to_llm"][0].content
                    for table in tables:
                        self.assertIn(table.page_content, prompt)
                        self.assertIn(table.metadata["section_heading"], prompt)
                    for wrong in ("WRONG_DEGREE_SUBJECT", "OBSOLETE_SUBJECT", "STALE_CURRICULUM"):
                        self.assertNotIn(wrong, prompt)
                    self.assertIn("<curriculum_subject_request>", prompt)
                    self.assertNotIn("<standalone_topic_overview>", prompt)
                    self.assertEqual(result["messages_to_llm"][-1].content, question)
                    self.assertEqual([doc.metadata["chunk_id"] for doc in h.ns["build_citations"].call_args.args[0]],
                                     [f"bsit-{index}" for index in range(8)])

    def test_requested_it_year_can_be_discovered_from_other_current_semesters(self):
        h = self.h
        source = "IT_Curriculum_2026.docx"
        degree = "Bachelor of Science in Information Technology"
        header = "COURSE CODE | COURSE TITLE | UNITS\n"
        introductory = fixtures.document(source, header + "IT 101 | Introduction | 3", "first",
                                         section_heading=degree + " / First Year / First Semester")
        third = [fixtures.document(source, header + f"IT {301 + index} | Current Subject {index} | 3", f"third-{index}",
                                   section_heading=degree + f" / Third Year / {semester} Semester")
                 for index, semester in enumerate(("First", "Second"))]
        old = fixtures.document(fixtures.BASE, header + "IT 300 | OLD_THIRD_YEAR_SUBJECT | 3", "old-third",
                                section_heading=degree + " / Third Year / First Semester")
        hybrid = h.install([introductory, *third], baseline=[old])

        def filtered(query, metadata_filter):
            for semester, doc in zip(("First", "Second"), third):
                if (f"Third Year {semester} Semester" in query and "BSIT" in query
                        and metadata_filter == {"source": {"$eq": source}}):
                    return [doc]
            return [introductory]

        hybrid.filtered = filtered
        hybrid.ensemble.invoke.side_effect = lambda query: [introductory, old]
        h.ns["query_optimizer_model"].invoke.side_effect = RuntimeError("offline")
        result = h.chat("it 3rd year subjects")
        self.assertEqual(set(result["citations"]), {"third-0", "third-1"}, h.output.getvalue())
        self.assertNotIn("OLD_THIRD_YEAR_SUBJECT", result["messages_to_llm"][0].content)


if __name__ == "__main__":
    unittest.main()
