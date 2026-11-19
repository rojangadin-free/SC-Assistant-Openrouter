"""Offline standalone-topic retrieval and answer-scope regressions."""
import os
import unittest
from unittest.mock import Mock, patch

import test_newer_retrieval as fixtures


class TopicOverviewTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
