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
        with patch.object(spelling, "_suggest", side_effect=lambda word, vocab: "crime" if word == "crim" else None):
            self.assertEqual(spelling.correct_query("dean of crim"), "")
            self.assertEqual(spelling.correct_query("dean of crim who si?"), "dean of crim who is?")
            self.assertEqual(spelling.correct_query("pls dean of crim who si?"), "pls dean of crim who is?")
            self.assertEqual(spelling.correct_query("crim dean pls?"), "")
            self.assertEqual(spelling.correct_query("the crim rate"), "the crime rate")

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


if __name__ == "__main__":
    unittest.main()
