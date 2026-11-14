"""Compare seed labels without importing its AWS code; preserve legacy DB claims."""

import ast
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
from test_student_linking import student_adapter

from rag import dummy_students as demo
from rag.student_subjects import (
    BSIT_SUBJECTS,
    CATALOG_SOURCE,
    PROGRAM_CODE,
    PROGRAM_NAME,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_LISTS = {
    (1, 1): "SUBJECTS_1ST_YEAR_1ST_SEM", (1, 2): "SUBJECTS_1ST_YEAR_2ND_SEM",
    (2, 1): "SUBJECTS_2ND_YEAR_1ST_SEM", (2, 2): "SUBJECTS_2ND_YEAR_2ND_SEM",
    (3, 1): "SUBJECTS_3RD_YEAR_1ST_SEM", (3, 2): "SUBJECTS_3RD_YEAR_2ND_SEM",
}

# Frozen original schema/data, deliberately independent of the version-2 builder.
V1_SCHEMA = (
    """CREATE TABLE demo_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        data_source TEXT NOT NULL CHECK (data_source = 'sqlite_demo'),
        start_id INTEGER NOT NULL CHECK (start_id BETWEEN 100000 AND 3000000),
        end_id INTEGER NOT NULL CHECK (end_id BETWEEN start_id AND 3000000),
        row_count INTEGER NOT NULL CHECK (row_count BETWEEN 0 AND end_id - start_id + 1),
        complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
        CHECK (complete = 0 OR row_count = end_id - start_id + 1)
    )""",
    """CREATE TABLE demo_courses (
        course_id INTEGER PRIMARY KEY,
        subject_code TEXT NOT NULL UNIQUE,
        subject_name TEXT NOT NULL,
        units INTEGER NOT NULL CHECK (units > 0),
        schedule TEXT NOT NULL,
        room TEXT NOT NULL,
        instructor TEXT NOT NULL,
        fee INTEGER NOT NULL CHECK (fee >= 0)
    )""",
    """CREATE TABLE students (
        student_number INTEGER PRIMARY KEY CHECK (student_number BETWEEN 100000 AND 3000000),
        program_id INTEGER NOT NULL CHECK (program_id BETWEEN 0 AND 2),
        year_level INTEGER NOT NULL CHECK (year_level BETWEEN 1 AND 4),
        section INTEGER NOT NULL CHECK (section BETWEEN 0 AND 2),
        semester INTEGER NOT NULL CHECK (semester IN (1, 2)),
        grade_1 INTEGER NOT NULL CHECK (grade_1 BETWEEN 100 AND 300),
        grade_2 INTEGER NOT NULL CHECK (grade_2 BETWEEN 100 AND 300),
        grade_3 INTEGER NOT NULL CHECK (grade_3 BETWEEN 100 AND 300),
        total_paid INTEGER NOT NULL CHECK (total_paid BETWEEN 0 AND 13000)
    )""",
    """CREATE TABLE account_links (
        pool_id TEXT NOT NULL CHECK (length(pool_id) BETWEEN 1 AND 256),
        uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
        student_number INTEGER NOT NULL UNIQUE REFERENCES students(student_number),
        PRIMARY KEY (pool_id, uid)
    ) WITHOUT ROWID""",
)
V1_COURSES = (
    (1, "DEMO101", "Demo Foundations", 3, "Demo Mon 09:00-12:00", "Demo A", "Demo Instructor A", 4500),
    (2, "DEMO102", "Demo Applied Studies", 3, "Demo Tue 09:00-12:00", "Demo B", "Demo Instructor B", 4500),
    (3, "DEMO103", "Demo Workshop", 2, "Demo Wed 09:00-11:00", "Demo C", "Demo Instructor C", 3000),
)


def legacy_database(path, *, count=6):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA application_id=1396917325")
        connection.execute("PRAGMA user_version=1")
        for sql in V1_SCHEMA:
            connection.execute(sql)
        connection.execute("INSERT INTO demo_metadata VALUES (1,1,'sqlite_demo',100000,100005,?,?)",
                           (count, int(count == 6)))
        connection.executemany("INSERT INTO demo_courses VALUES (?,?,?,?,?,?,?,?)", V1_COURSES)
        connection.executemany("INSERT INTO students VALUES (?,?,?,?,?,?,?,?,?)", (
            (100000 + i, i % 3, i % 4 + 1, i // 12 % 3, i // 4 % 2 + 1,
             100 + 25 * (i % 9), 100 + 25 * ((i + 3) % 9), 100 + 25 * ((i + 6) % 9),
             13000 * (i % 3) // 2) for i in range(count)
        ))
        if count == 6:
            connection.executemany("INSERT INTO account_links VALUES (?,?,?)", (
                ("pool-a", "owner", 100000), ("pool-a", "other-owner", 100001),
                ("pool-b", "owner", 100005),
            ))


def query(path, sql):
    with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(sql).fetchall()


def snapshot(path):
    tables = ("demo_metadata", "demo_courses", "students", "account_links")
    return {
        **{table: query(path, f"SELECT * FROM {table} ORDER BY 1,2") for table in tables},
        "schema": query(path, "SELECT type,name,sql FROM sqlite_master ORDER BY name"),
        "version": query(path, "PRAGMA user_version"),
        "application": query(path, "PRAGMA application_id"),
    }


class SeedCatalogTests(unittest.TestCase):
    def test_all_labels_and_units_match_the_seed_literals_exactly(self):
        tree = ast.parse((ROOT / "tools/seed_students.py").read_text(encoding="utf-8"))
        lists = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in SOURCE_LISTS.values():
                    lists[name] = ast.literal_eval(node.value)
        expected = {
            term: tuple((item["subject_code"], item["description"], item["units"]) for item in lists[name])
            for term, name in SOURCE_LISTS.items()
        }
        self.assertEqual(BSIT_SUBJECTS, expected)
        self.assertEqual([len(subjects) for subjects in expected.values()], [10, 10, 8, 8, 8, 9])
        self.assertEqual([sum(subject[2] for subject in subjects) for subjects in expected.values()],
                         [29, 28, 23, 23, 24, 27])
        for term in ((1, 2), (3, 2)):
            self.assertIn(("Math 02", "College Algebra", 3), BSIT_SUBJECTS[term])
        self.assertEqual(PROGRAM_NAME, "Bachelor of Science in Information Technology")
        self.assertEqual(PROGRAM_CODE, "BSIT")
        self.assertEqual(CATALOG_SOURCE, "tools/seed_students.py")

    def test_shared_catalog_contains_no_imports_or_executable_calls(self):
        tree = ast.parse((ROOT / "rag/student_subjects.py").read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)) for node in ast.walk(tree)))


class SeedMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sc_subject_migration_")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "students # original.sqlite3"
        self.backup = self.directory / "before # subjects.sqlite3"
        legacy_database(self.path)

    def upgrade(self):
        return demo.update_subject_catalog(self.path, backup_path=self.backup)

    def assert_current_subjects(self, record, term):
        self.assertEqual(record["program"], PROGRAM_NAME)
        self.assertEqual(record["program_code"], PROGRAM_CODE)
        self.assertEqual(record["subject_catalog_source"], CATALOG_SOURCE)
        self.assertIs(record["is_demo"], True)
        for key in ("enrolled_subjects", "grades", "tuition_breakdown"):
            actual = tuple((item["subject_code"], item["subject_name"]) for item in record[key])
            self.assertEqual(actual, tuple(subject[:2] for subject in BSIT_SUBJECTS[term]))
        for key in ("enrolled_subjects", "grades"):
            self.assertEqual([item["units"] for item in record[key]], [subject[2] for subject in BSIT_SUBJECTS[term]])
        self.assertEqual(record["total_tuition"], 12000)
        self.assertEqual(sum(item["fee"] for item in record["tuition_breakdown"]), 12000)
        self.assertEqual(record["net_assessment"], 13000)
        self.assertEqual(record["balance"], 13000 - record["total_paid"])

    def test_backup_and_migration_preserve_claims_ids_grades_and_payments(self):
        before = snapshot(self.path)
        result = self.upgrade()
        self.assertTrue(result["changed"])
        self.assertEqual((result["from_version"], result["to_version"], result["row_count"]), (1, 2, 6))
        self.assertEqual(Path(result["backup_path"]), self.backup)
        self.assertEqual(snapshot(self.backup), before)
        after = snapshot(self.path)
        self.assertEqual(after["account_links"], before["account_links"])
        self.assertEqual([(row[0], *row[5:]) for row in after["students"]],
                         [(row[0], *row[5:]) for row in before["students"]])
        self.assertEqual(len(after["demo_courses"]), 53)
        self.assertEqual(after["version"], [(2,)])
        database = demo.DummyStudentDatabase(self.path)
        for pool, uid, number in before["account_links"]:
            term = ((number - 100000) // 2 % 3 + 1, (number - 100000) % 2 + 1)
            record = database.get_by_uid(pool, uid)
            self.assertEqual(record["student_number"], str(number))
            self.assertEqual(record["student_id"], uid)
            self.assert_current_subjects(record, term)
            self.assertEqual(database.link_account(pool, uid, str(number)), "already_linked")
            self.assertEqual(database.link_account("different-pool", uid, str(number)), "conflict")
        self.assertEqual(database.link_account("pool-a", "owner", "100003"), "conflict")
        self.assertEqual(database.get_by_uid("pool-a", "owner")["student_number"], "100000")

    def test_legacy_record_remains_readable_until_explicit_upgrade(self):
        before = snapshot(self.path)
        record = demo.DummyStudentDatabase(self.path).get_by_uid("pool-a", "owner")
        self.assertEqual(record["student_number"], "100000")
        self.assertEqual([subject["subject_code"] for subject in record["enrolled_subjects"]],
                         ["DEMO101", "DEMO102", "DEMO103"])
        self.assertEqual(snapshot(self.path), before)

    def test_repeat_upgrade_is_a_noop_and_preserves_original_backup(self):
        self.upgrade()
        before, backup = snapshot(self.path), snapshot(self.backup)
        self.assertFalse(self.upgrade()["changed"])
        self.assertEqual(snapshot(self.path), before)
        self.assertEqual(snapshot(self.backup), backup)

    def test_existing_backup_is_never_overwritten(self):
        legacy_database(self.backup)
        before, backup = snapshot(self.path), snapshot(self.backup)
        with self.assertRaises((OSError, demo.DummyStudentDatabaseError)):
            self.upgrade()
        self.assertEqual(snapshot(self.path), before)
        self.assertEqual(snapshot(self.backup), backup)

    def test_migration_failure_rolls_back_all_metadata_and_roster_updates(self):
        before = snapshot(self.path)
        original_connect = sqlite3.connect

        def deny_roster_updates(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_authorizer(lambda action, table, _column, _db, _trigger:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE and table == "students" else sqlite3.SQLITE_OK)
            return connection

        with patch.object(sqlite3, "connect", side_effect=deny_roster_updates), \
                self.assertRaises(demo.DummyStudentDatabaseError):
            self.upgrade()
        self.assertEqual(snapshot(self.path), before)
        self.assertEqual(snapshot(self.backup), before)

    def test_incomplete_legacy_prefix_upgrades_then_resumes_without_clobbering(self):
        partial = self.directory / "partial.sqlite3"
        legacy_database(partial, count=4)
        original = query(partial, "SELECT student_number,grade_1,grade_2,grade_3,total_paid FROM students ORDER BY 1")
        result = demo.update_subject_catalog(partial, backup_path=self.directory / "partial-backup.sqlite3")
        self.assertEqual(result["row_count"], 4)
        self.assertEqual(query(partial, "SELECT complete FROM demo_metadata"), [(0,)])
        with self.assertRaises(demo.DummyStudentDatabaseError):
            demo.DummyStudentDatabase(partial).get_by_uid("pool", "owner")
        demo.create_database(partial, start_id=100000, end_id=100005, batch_size=2, resume=True)
        self.assertEqual(query(partial, "SELECT student_number,grade_1,grade_2,grade_3,total_paid FROM students ORDER BY 1")[:4],
                         original)
        self.assertEqual(query(partial, "SELECT row_count,complete FROM demo_metadata"), [(6, 1)])

    def test_new_generation_covers_all_six_seed_terms_and_formats_exact_labels(self):
        current = self.directory / "current.sqlite3"
        demo.create_database(current, start_id=100000, end_id=100005, batch_size=2)
        database = demo.DummyStudentDatabase(current)
        adapter, real_table = student_adapter()
        for offset in range(6):
            number, uid = str(100000 + offset), f"owner-{offset}"
            database.link_account("pool", uid, number)
            record = database.get_by_uid("pool", uid)
            term = (offset // 2 + 1, offset % 2 + 1)
            self.assertEqual((record["year_level"], record["semester"]), (term[0], "1st" if term[1] == 1 else "2nd"))
            self.assert_current_subjects(record, term)
            context = adapter["format_student_context"](record)
            self.assertIn("SYNTHETIC DEMO DATA", context)
            self.assertIn("copied exactly", context)
            self.assertIn(CATALOG_SOURCE, context)
            for code, name, _units in BSIT_SUBJECTS[term]:
                self.assertIn(code, context)
                self.assertIn(name, context)
        self.assertFalse(real_table.mock_calls)


if __name__ == "__main__":
    unittest.main()
