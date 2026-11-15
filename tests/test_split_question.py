"""Offline question-boundary tests; no models, stores or application startup."""

import ast
import re
import unittest
from pathlib import Path


def load_splitter():
    path = Path(__file__).resolve().parents[1] / "rag" / "chain.py"
    names = {"_STOPWORDS", "_QUESTION_WORDS", "_DETERMINERS", "MIN_PART_TERMS", "MAX_PARTS"}
    functions = {"_content_terms", "split_question"}
    nodes = []
    for node in ast.parse(path.read_text(encoding="utf-8-sig")).body:
        if ((isinstance(node, ast.FunctionDef) and node.name in functions)
                or (isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in names for target in node.targets
                ))):
            nodes.append(node)
    namespace = {"re": re, "List": list}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)  # noqa: S102
    return namespace["split_question"], namespace["MAX_PARTS"]


split_question, MAX_PARTS = load_splitter()


class QuestionSplitTests(unittest.TestCase):
    def test_reported_correction_is_one_named_college(self):
        for question in (
            "i mean college of art and science",
            "i mean college of arts and science",
            "I mean, College of Arts and Sciences.",
            "Actually, the College of Liberal Arts and Sciences",
        ):
            with self.subTest(question=question):
                self.assertEqual(split_question(question), [])

    def test_unseen_institution_office_and_program_names_stay_intact(self):
        for question in (
            "College of Business and Management",
            "Who is the dean of the College of Arts and Sciences?",
            "What programs are in the College of Information Technology and Allied Sciences?",
            "Tell me about the College of Space, Earth and Ocean Studies",
            "College of Arts and the Sciences",
            "Department of Research, Development and Extension",
            "Office of Planning, Budget, and Administration",
            "Office of the President and Directors",
            "Bachelor of Science in Information and Communication Technology",
            "Master of Arts in Leadership and Management",
            "School of Speech or Language Studies",
        ):
            with self.subTest(question=question):
                self.assertEqual(split_question(question), [])

    def test_a_named_college_does_not_hide_the_next_explicit_question(self):
        first = "Who is the dean of the College of Arts and Sciences"
        second = "who is the principal of highschool"
        for separator in (" and ", ", ", ", and ", "; ", " or "):
            with self.subTest(separator=separator):
                self.assertEqual(split_question(first + separator + second + "?"), [first, second])

    def test_shared_role_stem_stops_before_the_college_name(self):
        first = "Who is the dean of the College of Arts and Sciences"
        for second in ("College of Business and Management", "the College of Business and Management"):
            with self.subTest(second=second):
                self.assertEqual(split_question(first + " and " + second + "?"),
                                 [first, "Who is the dean of " + second])

    def test_separate_roles_keep_their_own_nouns(self):
        first = "Who is the dean of the College of Arts and Sciences"
        for second in ("the principal of highschool", "principal of highschool", "the president",
                       "the vice president", "the office head for admissions"):
            with self.subTest(second=second):
                self.assertEqual(split_question(first + " and " + second + "?"),
                                 [first, "Who is " + second])

    def test_original_principal_and_dean_enumeration_still_splits(self):
        self.assertEqual(split_question("who are the principal of highschool, elementary and who is the dean of citas?"), [
            "who are the principal of highschool", "who are the principal of elementary", "who is the dean of citas",
        ])
        self.assertEqual(split_question("who is the president of samar college and the dean of citas and the principal of highschool?"), [
            "who is the president of samar college", "who is the dean of citas", "who is the principal of highschool",
        ])

    def test_independent_commands_and_named_units_still_split(self):
        self.assertEqual(split_question("College of Arts and Sciences and College of Business and Management"),
                         ["College of Arts and Sciences", "College of Business and Management"])
        self.assertEqual(split_question("Tell me about the College of Arts and Sciences and explain admission requirements"),
                         ["Tell me about the College of Arts and Sciences", "explain admission requirements"])
        self.assertEqual(split_question("College of Arts and Sciences; tuition fees"),
                         ["College of Arts and Sciences", "tuition fees"])

    def test_existing_general_requests_and_limits_are_preserved(self):
        self.assertEqual(split_question("What are admission requirements and tuition fees?"),
                         ["What are admission requirements", "What are tuition fees"])
        self.assertEqual(split_question("When is enrollment and how much is tuition?"),
                         ["When is enrollment", "how much is tuition"])
        for question in (None, "", "   ", "college department", "i lost my id"):
            with self.subTest(question=question):
                self.assertEqual(split_question(question), [])
        parts = split_question("What are fees; who is the dean; when is enrollment; where is the office; why is it closed")
        self.assertEqual(len(parts), MAX_PARTS)


if __name__ == "__main__":
    unittest.main()
