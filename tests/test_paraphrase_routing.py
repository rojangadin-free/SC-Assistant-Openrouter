import ast
import json
import re
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401

from rag.query_intent import known_query_intents
from rag.source_policy import grading_queries, select_documents
from rag.unit_identity import requested_unit


CASES_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "paraphrase_cases.jsonl"

DOCUMENT_TEXT = {
    "graduate school grading": "GRADING SYSTEM OF THE GS. Graduate School averaging system.\nNumerical Rating | Percentage Equivalent | Adjective Rating\n1.0 | 98-100 | Excellent\n3.0 | 75 | Passing",
    "college grading": "GRADING SYSTEM. College students use this grading scale and numerical rating table.",
    "Dean's List requirements": "ACADEMIC AWARDS. Dean's List eligibility requirements and GWA recognition.",
    "enrollment procedure": "Enrollment Procedure. New Students and Transferees. Step 1: Submit credentials. Step 2: Follow the enrollment process.",
    "student ID replacement": "Radio Frequency Identification (RFID). Lost RFID must be reported to Security Management Office. School RFID replacement price.",
    "tuition and sibling discount": "SCHOOL SCHOLARSHIP. Members of the Family Nucleus receive a tuition discount for siblings enrolled together.",
    "academic calendar": "CITAS CALENDAR OF ACTIVITIES A.Y. 2026-2027. Opening of 2nd Semester: December 1.",
    "campus location": "Footer & Contact Information. Samar Colleges, Inc. | Catbalogan City, Samar, Mabini Avenue.",
    "beta next-semester enrollment date": "CITAS CALENDAR OF ACTIVITIES A.Y. 2026-2027. Opening of 2nd Semester: December 1.",
    "SCTI assessment qualifications": "SCTI PROGRAM OFFERED. REGISTERED ASSESSMENT CENTER. Qualification Title | Assessment Fee\nCookery NC II | PHP 1,300.00",
    "beta pending admission credentials": "General Guidelines. Applicants must submit complete admission requirements on or before the enrollment schedule. Transferees and returnees must present valid credentials.",
}

DISTRACTOR_TEXT = {
    "graduate school grading": "College grading system and undergraduate numerical rating.",
    "college grading": "GRADING SYSTEM OF THE GS. Graduate School grading scale and grades.",
    "Dean's List requirements": "COLLEGE DEANS roster. Names and office titles only.",
    "enrollment procedure": "Admission Policy. Applicants submit complete requirements before enrollment.",
    "student ID replacement": "School uniform and validated ID entrance rules.",
    "tuition and sibling discount": "Academic scholarship and athletic scholarship information.",
    "academic calendar": "School Calendar of Activities. Calendar details will be posted here.",
    "campus location": "Office Locations. Student Affairs office, Ground Floor, New Building.",
    "beta next-semester enrollment date": "School Calendar of Activities. Calendar details will be posted here.",
    "SCTI assessment qualifications": "Samar College degree programs: Bachelor of Science in Criminology.",
    "beta pending admission credentials": "Office Locations. Registrar on the first floor of the building.",
}


class ParaphraseRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with CASES_PATH.open(encoding="utf-8") as cases_file:
            cls.cases = [json.loads(line) for line in cases_file if line.strip()]

    def test_live_evaluator_requires_expected_source_and_section_in_the_same_chunk(self):
        path = CASES_PATH.parents[1] / "tools" / "eval_retrieval.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        namespace = {"Path": Path, "re": re}
        check_function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "check")
        exec(compile(ast.Module(body=[check_function], type_ignores=[]), str(path), "exec"), namespace)
        check = namespace["check"]
        expected = SimpleNamespace(page_content="Grading System", metadata={"source": "folder/Handbook.pdf"})
        other = SimpleNamespace(page_content="Grading System", metadata={"source": "Other.pdf"})
        wrong_section = SimpleNamespace(page_content="Admission Policy", metadata={"source": "Handbook.pdf"})
        self.assertEqual(check([other, expected], ["Grading"], "Handbook.pdf", "Grading System")["expected source + section"], 2)
        self.assertIsNone(check([other, wrong_section], ["Grading"], "Handbook.pdf", "Grading System")["expected source + section"])

    def test_paraphrases_keep_expected_source_and_section_through_routing(self):
        self.assertEqual(len(self.cases), len(DOCUMENT_TEXT))
        today = date(2026, 11, 21)

        for case in self.cases:
            name = case["name"]
            with self.subTest(case=name):
                expected = SimpleNamespace(
                    page_content=DOCUMENT_TEXT[name],
                    metadata={
                        "source": case["expected_source"],
                        "section_heading": case["expected_section"],
                        "collection_heading": (
                            "Graduate School / " + case["expected_section"]
                            if name == "graduate school grading" else case["expected_section"]
                        ),
                    },
                )
                distractor = SimpleNamespace(
                    page_content=DISTRACTOR_TEXT[name],
                    metadata={"source": "unrelated-reference.pdf", "section_heading": "Unrelated Section"},
                )
                queries = [case["question"], *case["rewrites"]]
                self.assertGreaterEqual(len(queries), 6)
                self.assertLessEqual(len(queries), 8)

                for query in queries:
                    with self.subTest(query=query):
                        intents = known_query_intents(query)
                        if query == case["question"] and case.get("routing_intent"):
                            self.assertIn(case["routing_intent"], intents)
                        grade_probes = grading_queries(query)
                        if query == case["question"] and case.get("routing_intent") == "grading":
                            self.assertTrue(grade_probes)
                        unit = requested_unit(query)
                        if unit:
                            self.assertTrue(unit.name.strip())

                        selection = select_documents(
                            [expected, distractor], [query], dates={}, uploads={}, today=today,
                            enforce_sources=False,
                        )
                        self.assertTrue(any(
                            document.metadata["source"] == case["expected_source"]
                            and case["expected_section"].casefold() in document.metadata["section_heading"].casefold()
                            for document in selection.documents
                        ))


if __name__ == "__main__":
    unittest.main()
