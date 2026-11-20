"""Offline Dean's List intent, retrieval, and answer-prompt regressions."""
import os
import unittest
from unittest.mock import Mock, patch

import test_newer_retrieval as fixtures


class DeansListTests(unittest.TestCase):
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

    def test_academic_name_is_not_a_personnel_roster_request(self):
        policy = self.harness.policy
        for question in ("deans list", "dean's list", "Dean\u2019s List", "deans list pls",
                         "deans lister", "Dean's Lister", "Dean\u2019s Lister", "DEANS LISTERS",
                         "how do I become a deans lister?", "deans lister requirements",
                         "what is the deans list?", "how do I qualify for Dean's List?",
                         "Dean's List GWA requirements", "college Dean's List"):
            with self.subTest(question=question):
                self.assertEqual(len(policy.deans_list_queries(question)), 2)
                self.assertEqual(policy.leadership_roster_queries(question), [])
                self.assertEqual(policy.leadership_guidance(question), "")
        for question in (None, "", 42, "list of deans", "list all college deans",
                         "who are the deans?", "dean of BSCRIM", "Latin honors requirements",
                         "deans listing of personnel", "deans listening session", "the listed deans"):
            self.assertEqual(policy.deans_list_queries(question), [])
        for question in ("list all college deans", "who are the deans?", "give me the list of deans"):
            self.assertTrue(policy.leadership_roster_queries(question))

    def test_only_substantive_academic_evidence_survives_both_source_modes(self):
        policy = self.harness.policy
        mention = fixtures.document(fixtures.BASE, "A student must have been in the Dean's List for four consecutive "
                                    "semesters for the Special Citation Award for Commendable Academic Achievement.", "citation", page=56)
        scholarship = fixtures.document(fixtures.BASE, "Academic scholars must be included in the Dean's list prior "
                                        "to inclusion in the academic scholar's list.", "scholarship", page=94)
        roster = fixtures.document("Organizational_Structure_2026.docx", "Deans List\nCollege of Education: "
                                   "Dean Alex Example\nCollege of Business: Dean Pat Example", "roster")
        title = fixtures.document("Academic_Recognition.docx", "Dean's List", "title")
        unrelated = fixtures.document(fixtures.BASE, "Summa Cum Laude. GWA 1.00 to 1.20.", "latin")
        docs = [roster, title, unrelated, mention, scholarship]
        self.assertEqual(policy.deans_list_documents(docs), [mention, scholarship])
        recipient = fixtures.document(fixtures.BASE, "Dean's Listers receive academic recognition each semester.", "listers")
        self.assertEqual(policy.deans_list_documents([roster, title, unrelated, recipient]), [recipient])
        for mode in (True, False):
            selection = policy.select_documents(docs, ["deans list", "college deans named roster"],
                                                dates=self.harness.dates, uploads={roster.metadata["source"]: fixtures.STAMP},
                                                enforce_sources=mode)
            self.assertEqual(selection.documents, [mention, scholarship])
            self.assertFalse(selection.restricted)

    def test_academic_probes_recover_evidence_despite_wrong_optimizer_and_context_cap(self):
        h = self.harness
        mention = fixtures.document(fixtures.BASE, "Special Citation Award for Commendable Academic Achievement: "
                                    "Dean's List for four consecutive semesters is a prerequisite.", "citation", page=56)
        scholarship = fixtures.document(fixtures.BASE, "Academic scholarship requires inclusion in the Dean's List "
                                        "prior to inclusion in the academic scholar's list.", "scholarship", page=94)
        roster = fixtures.document("Organizational_Structure_2026.docx", "College of Education: Dean Alex Example", "roster")
        fixtures.definitions("src/prompt.py", {"system_prompt"}, h.ns)
        roles = h.module("roles")
        h.ns.update(resolve_role=roles.resolve_role, role_block=roles.role_block)
        for question in ("deans list", "Dean\u2019s List", "deans list pls", "Dean's List requirements",
                         "deans lister", "Dean's Lister", "Dean\u2019s Lister", "deans listers",
                         "deans lister requirements"):
            for mode in ("true", "false"):
                for optimizer_failed in (True, False):
                    with self.subTest(question=question, mode=mode, optimizer_failed=optimizer_failed), patch.dict(
                        os.environ, {"RAG_SOURCE_FILTER_ENABLED": mode}
                    ), patch.dict(h.ns, {"FINAL_TOP_K": 2}):
                        hybrid = h.install([roster], baseline=[])
                        hybrid.ensemble.invoke.side_effect = lambda query: [mention, scholarship] if "academic" in query.lower() else [roster]
                        h.ns["query_optimizer_model"].invoke = Mock(
                            side_effect=RuntimeError("optimizer unavailable") if optimizer_failed else None,
                            return_value=fixtures.SimpleNamespace(content="college deans named roster"),
                        )
                        result = h.chat(question, history=[{"role": "assistant", "content": "There is no Dean's List document."}], role="guest")
                        self.assertEqual(result["citations"], ["citation", "scholarship"], h.output.getvalue())
                        prompt = result["messages_to_llm"][0].content
                        self.assertIn("<deans_list_academic_request>", prompt)
                        self.assertNotIn("<dean_list_request>", prompt)
                        self.assertNotIn("Dean Alex Example", prompt)
                        self.assertNotIn("There is no Dean's List document.", prompt)
                        self.assertIn("not standalone entry criteria", prompt)
                        self.assertIn("Do not describe retrieval limitations", prompt)
                        self.assertNotIn("State the retrieval limitation", prompt)
                        self.assertNotIn("politely remind them at the end", prompt)
                        self.assertNotIn("personalised help is welcome", prompt)
                        self.assertIn("<answer_scope>", prompt)
                        if "requirements" not in question:
                            self.assertIn("<deans_list_overview>", prompt)
                        self.assertEqual(result["messages_to_llm"][-1].content, question)

    def test_lister_is_not_changed_to_listed_by_the_spelling_corpus(self):
        spelling = self.harness.spelling
        vocab = spelling._Vocabulary({**spelling._vocabulary().freq, "listed": 1000})
        with patch.object(spelling, "_vocabulary", return_value=vocab):
            for question in ("deans lister", "Dean's Lister", "deans listers", "deans lister requirements"):
                with self.subTest(question=question):
                    self.assertEqual(spelling.correct_query(question), "")
                    self.assertEqual(spelling.misspellings(question), [])


if __name__ == "__main__":
    unittest.main()
