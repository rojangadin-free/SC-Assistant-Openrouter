"""Offline stdlib tests; only tiny, temporary synthetic rosters are generated."""

import inspect
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag import dummy_students as demo


def _process_claim(path, uid, barrier, results):
    try:
        barrier.wait(timeout=20)
        results.put((uid, demo.DummyStudentDatabase(path).link_account("pool", uid, "100000")))
    except (demo.DummyStudentDatabaseError, ValueError, threading.BrokenBarrierError) as exc:
        results.put((uid, f"ERROR: {type(exc).__name__}: {exc}"))


class DummyStudentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sc_dummy_students_")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "dummy # demo.sqlite3"

    def create(self, **kwargs):
        return demo.create_database(self.path, start_id=100000, end_id=100005, batch_size=2, **kwargs)

    def create_legacy(self, row_count=6):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            for sql in demo._LEGACY_SCHEMA.values():
                connection.execute(sql)
            connection.execute("PRAGMA application_id = 1396917325")
            connection.execute("PRAGMA user_version = 1")
            connection.execute("INSERT INTO demo_metadata VALUES (1, 1, 'sqlite_demo', 100000, 100005, ?, ?)",
                               (row_count, int(row_count == 6)))
            connection.executemany("INSERT INTO demo_courses VALUES (?, ?, ?, ?, ?, ?, ?, ?)", demo._LEGACY_COURSES)
            connection.executemany("INSERT INTO students VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (demo._student_values(number, version=1) for number in range(100000, 100000 + row_count)))
            connection.commit()

    def query(self, sql, parameters=()):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute(sql, parameters).fetchall()

    def mutate(self, sql, parameters=()):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)) as connection:
            connection.execute(sql, parameters)
            connection.commit()

    def interrupt_after(self, count):
        def interrupt(done, total):
            if done == count:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.create(progress=interrupt)

    def assert_rejected(self):
        database = demo.DummyStudentDatabase(self.path)
        with self.assertRaises(demo.DummyStudentDatabaseError):
            database.get_by_uid("pool", "uid")
        with self.assertRaises(demo.DummyStudentDatabaseError):
            database.link_account("pool", "uid", "100000")
        with self.assertRaises(demo.DummyStudentDatabaseError):
            self.create(resume=True)

    def test_defaults_describe_full_inclusive_range_without_generating_it(self):
        parameters = inspect.signature(demo.create_database).parameters
        self.assertEqual(parameters["start_id"].default, 100000)
        self.assertEqual(parameters["end_id"].default, 3000000)
        self.assertEqual(parameters["batch_size"].default, 10000)
        self.assertEqual(parameters["end_id"].default - parameters["start_id"].default + 1, 2900001)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_options_are_rejected_before_file_creation(self):
        invalid = [
            {"start_id": value} for value in (True, False, "100000", 100000.0, None, 99999, 3000001)
        ] + [
            {"end_id": value} for value in (True, False, "100005", 100005.0, None, 99999, 3000001)
        ] + [
            {"batch_size": value} for value in (True, False, "2", 2.0, None, 0, -1, 100001)
        ] + [{"start_id": 100006}, {"resume": "yes"}, {"progress": "not callable"}]
        for overrides in invalid:
            options = {"start_id": 100000, "end_id": 100005, "batch_size": 2, **overrides}
            with self.subTest(options=options), patch.object(demo.os, "open") as opening:
                with self.assertRaises(ValueError):
                    demo.create_database(self.path, **options)
                opening.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_committed_progress_and_compact_physical_rows(self):
        observed = []

        def progress(done, total):
            observed.append((done, total))
            self.assertEqual(self.query("SELECT COUNT(*) FROM students")[0][0], done)
            self.assertEqual(self.query("SELECT row_count, complete FROM demo_metadata")[0], (done, int(done == total)))

        result = self.create(progress=progress)
        self.assertEqual(observed, [(0, 6), (2, 6), (4, 6), (6, 6)])
        self.assertEqual(result, {"path": str(self.path), "start_id": 100000, "end_id": 100005,
                                  "row_count": 6, "complete": True})
        self.assertEqual(self.query("SELECT student_number FROM students ORDER BY student_number"),
                         [(number,) for number in range(100000, 100006)])
        columns = self.query("PRAGMA table_info(students)")
        self.assertTrue(all(column[2] == "INTEGER" for column in columns))
        self.assertEqual(len(self.query("SELECT * FROM demo_courses")), 53)
        self.assertEqual(self.query("SELECT year_level, semester, COUNT(*) FROM demo_courses GROUP BY year_level, semester"),
                         [(1, 1, 10), (1, 2, 10), (2, 1, 8), (2, 2, 8), (3, 1, 8), (3, 2, 9)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM demo_courses WHERE subject_code = ?", ("Math 02",)), [(2,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM account_links"), [(0,)])
        self.assertEqual(self.query("PRAGMA journal_mode"), [("wal",)])
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_singleton_ranges_include_both_allowed_boundaries(self):
        for number in (100000, 3000000):
            path = self.directory / f"boundary-{number}.sqlite3"
            result = demo.create_database(path, start_id=number, end_id=number, batch_size=1)
            self.assertEqual(result["row_count"], 1)
            database = demo.DummyStudentDatabase(path)
            self.assertEqual(database.link_account("pool", "uid", str(number)), "linked")
            self.assertEqual(database.get_by_uid("pool", "uid")["student_number"], str(number))

    def test_record_shape_markers_determinism_and_coherent_finances(self):
        self.create()
        database = demo.DummyStudentDatabase(self.path)
        statuses = set()
        sensitive = {"email", "password", "token", "birthday", "address", "phone_number", "health_record",
                     "father_name", "mother_name", "monthly_family_income", "emergency_contact_name"}
        for number in range(100000, 100006):
            uid = f"uid-{number}"
            self.assertEqual(database.link_account("pool", uid, str(number)), "linked")
            record = database.get_by_uid("pool", uid)
            self.assertEqual(record["student_id"], uid)
            self.assertEqual(record["student_number"], str(number))
            self.assertIs(record["is_demo"], True)
            self.assertEqual(record["data_source"], "sqlite_demo")
            self.assertEqual(record["full_name"], f"Demo Student {number}")
            offset = number - 100000
            term = (offset // 2 % 3 + 1, offset % 2 + 1)
            catalog = demo.BSIT_SUBJECTS[term]
            self.assertEqual(record["program"], demo.PROGRAM_NAME)
            self.assertEqual(record["program_code"], "BSIT")
            self.assertEqual(record["subject_catalog_source"], "tools/seed_students.py")
            self.assertEqual(record["section"], f"BSIT-{term[0]}A")
            self.assertEqual(record["year_level"], term[0])
            self.assertIn(record["semester"], ("1st", "2nd"))
            self.assertEqual(record["school_year"], "2026-2027")
            self.assertFalse(sensitive.intersection(record))
            self.assertEqual(len(record["enrolled_subjects"]), len(catalog))
            self.assertEqual(tuple((subject["subject_code"], subject["subject_name"], subject["units"])
                                   for subject in record["enrolled_subjects"]), catalog)
            seeds = self.query("SELECT grade_1, grade_2, grade_3 FROM students WHERE student_number = ?", (number,))[0]
            for position, (subject, grade) in enumerate(zip(record["enrolled_subjects"], record["grades"])):
                self.assertEqual(set(subject), {"subject_code", "subject_name", "units", "schedule", "room", "instructor"})
                self.assertEqual(set(grade), {"subject_code", "subject_name", "units", "prelim", "midterm",
                                             "semi_final", "final", "final_grade", "remarks"})
                for field in ("schedule", "room", "instructor"):
                    self.assertTrue(subject[field].lower().startswith("demo"))
                self.assertEqual(subject["subject_code"], grade["subject_code"])
                self.assertEqual(grade["final_grade"], seeds[position % 3] / 100)
                self.assertEqual(grade["final_grade"], sum(grade[key] for key in ("prelim", "midterm", "semi_final", "final")) / 4)
            weighted = sum(item["units"] * item["final_grade"] for item in record["grades"])
            self.assertEqual(record["gpa"], round(weighted / sum(item["units"] for item in record["grades"]), 2))
            self.assertEqual(record["total_tuition"], sum(item["fee"] for item in record["tuition_breakdown"]))
            self.assertEqual(record["total_tuition"], 12000)
            units_total = sum(units for _, _, units in catalog)
            for (_, _, units), item in zip(catalog[:-1], record["tuition_breakdown"][:-1]):
                self.assertEqual(item["fee"], 12000 * units // units_total)
            self.assertEqual(record["tuition_breakdown"][-1]["fee"],
                             12000 - sum(item["fee"] for item in record["tuition_breakdown"][:-1]))
            self.assertEqual(record["gross_total"], record["total_tuition"] + sum(record["misc_fees"].values()))
            self.assertEqual(record["net_assessment"], record["gross_total"] - record["scholarship_discount"])
            self.assertEqual(record["balance"], record["net_assessment"] - record["total_paid"])
            for key in ("total_tuition", "net_assessment", "total_paid", "balance", "minimum_payment"):
                self.assertGreaterEqual(record[key], 0)
            statuses.add(record["payment_status"])
        self.assertEqual(statuses, {"Paid in Full", "Partially Paid", "Unpaid"})
        other = self.directory / "different-range.sqlite3"
        demo.create_database(other, start_id=100005, end_id=100007, batch_size=1)
        other_database = demo.DummyStudentDatabase(other)
        other_database.link_account("pool", "uid-100005", "100005")
        self.assertEqual(database.get_by_uid("pool", "uid-100005"), other_database.get_by_uid("pool", "uid-100005"))

    def test_existing_files_are_never_clobbered_or_deleted(self):
        self.path.write_bytes(b"existing unrelated content")
        with patch.object(demo.sqlite3, "connect") as connect, self.assertRaises(FileExistsError):
            self.create()
        connect.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"existing unrelated content")
        self.assert_rejected()
        self.assertEqual(self.path.read_bytes(), b"existing unrelated content")

    def test_constructor_and_missing_database_operations_never_create_a_file(self):
        with patch.object(demo.sqlite3, "connect") as connect:
            database = demo.DummyStudentDatabase(self.path)
        connect.assert_not_called()
        for operation in (lambda: database.get_by_uid("pool", "uid"),
                          lambda: database.link_account("pool", "uid", "100000"),
                          lambda: self.create(resume=True)):
            with self.assertRaises(demo.DummyStudentDatabaseError):
                operation()
            self.assertEqual(list(self.directory.iterdir()), [])

    def test_complete_resume_is_a_noop_and_preserves_claims(self):
        original = self.create()
        database = demo.DummyStudentDatabase(self.path)
        database.link_account("pool", "uid", "100000")
        record = database.get_by_uid("pool", "uid")
        before = self.path.read_bytes()
        progress = []
        self.assertEqual(self.create(resume=True, progress=lambda done, total: progress.append((done, total))), original)
        self.assertEqual(progress, [(6, 6)])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(database.get_by_uid("pool", "uid"), record)

    def test_callback_interrupt_retains_only_committed_progress_and_resumes(self):
        self.interrupt_after(2)
        self.assertEqual(self.query("SELECT row_count, complete FROM demo_metadata"), [(2, 0)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM students"), [(2,)])
        database = demo.DummyStudentDatabase(self.path)
        with self.assertRaises(demo.DummyStudentDatabaseError):
            database.get_by_uid("pool", "uid")
        with self.assertRaises(demo.DummyStudentDatabaseError):
            database.link_account("pool", "uid", "100000")
        progress = []
        result = demo.create_database(self.path, start_id=100000, end_id=100005, batch_size=3,
                                      resume=True, progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(progress, [(2, 6), (5, 6), (6, 6)])
        self.assertTrue(result["complete"])
        self.assertEqual(database.link_account("pool", "uid", "100000"), "linked")

    def test_interrupt_before_first_batch_can_resume_an_empty_initialized_database(self):
        self.interrupt_after(0)
        self.assertEqual(self.query("SELECT row_count, complete FROM demo_metadata"), [(0, 0)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM students"), [(0,)])
        self.assertTrue(self.create(resume=True)["complete"])

    def test_interrupt_inside_batch_rolls_back_rows_and_metadata_together(self):
        original = demo._student_values

        def fail_mid_batch(number):
            if number == 100003:
                raise KeyboardInterrupt
            return original(number)

        with patch.object(demo, "_student_values", side_effect=fail_mid_batch), self.assertRaises(KeyboardInterrupt):
            self.create()
        self.assertEqual(self.query("SELECT row_count, complete FROM demo_metadata"), [(2, 0)])
        self.assertEqual(self.query("SELECT student_number FROM students ORDER BY student_number"), [(100000,), (100001,)])
        self.assertTrue(self.create(resume=True)["complete"])

    def test_sqlite_failure_inside_batch_rolls_back_and_closes_the_connection(self):
        original = demo._student_values

        def duplicate(number):
            return original(100002 if number == 100003 else number)

        with patch.object(demo, "_student_values", side_effect=duplicate), self.assertRaises(demo.DummyStudentDatabaseError):
            self.create()
        self.assertEqual(self.query("SELECT row_count, complete FROM demo_metadata"), [(2, 0)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM students"), [(2,)])
        self.assertTrue(self.create(resume=True)["complete"])

    def test_final_callback_exception_leaves_a_completed_database(self):
        self.interrupt_after(6)
        self.assertEqual(self.query("SELECT row_count, complete FROM demo_metadata"), [(6, 1)])
        database = demo.DummyStudentDatabase(self.path)
        self.assertEqual(database.link_account("pool", "uid", "100000"), "linked")
        self.assertTrue(self.create(resume=True)["complete"])

    def test_resume_rejects_a_different_range_without_changing_data(self):
        self.interrupt_after(2)
        before = self.path.read_bytes()
        with self.assertRaises(demo.DummyStudentDatabaseError):
            demo.create_database(self.path, start_id=100000, end_id=100006, resume=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_resume_rejects_holes_even_when_metadata_row_count_matches(self):
        self.interrupt_after(4)
        self.mutate("DELETE FROM students WHERE student_number = ?", (100001,))
        self.mutate("UPDATE demo_metadata SET row_count = 3")
        self.assert_rejected()
        self.assertEqual(self.query("SELECT COUNT(*) FROM students"), [(3,)])

    def test_resume_detects_missing_rows_and_application_never_claims_them(self):
        self.create()
        self.mutate("DELETE FROM students WHERE student_number = ?", (100001,))
        with self.assertRaises(demo.DummyStudentDatabaseError):
            self.create(resume=True)
        database = demo.DummyStudentDatabase(self.path)
        self.assertEqual(database.link_account("pool", "uid", "100001"), "not_found")
        self.assertIsNone(database.get_by_uid("pool", "uid"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM account_links"), [(0,)])

    def test_empty_corrupt_and_unrecognized_databases_are_rejected(self):
        for contents in (b"", b"SQLite format 3\x00broken", b"not a SQLite database"):
            with self.subTest(contents=contents):
                self.path.write_bytes(contents)
                self.assert_rejected()
                self.assertEqual(self.path.read_bytes(), contents)
        self.path = self.directory / "unrelated.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
        self.assert_rejected()

    def test_version_schema_and_shared_course_changes_are_rejected(self):
        changes = ("PRAGMA user_version = 999", "PRAGMA application_id = 0",
                   "ALTER TABLE students ADD COLUMN unknown TEXT",
                   "CREATE TABLE sqlitextra (value TEXT)",
                   "UPDATE demo_courses SET subject_name = 'Not the catalog' WHERE year_level = 1 AND semester = 1 AND position = 1")
        for index, sql in enumerate(changes):
            with self.subTest(sql=sql):
                self.path = self.directory / f"incompatible-{index}.sqlite3"
                self.create()
                self.mutate(sql)
                self.assert_rejected()

    def test_invalid_persisted_record_and_dangling_claim_fail_closed(self):
        self.create()
        database = demo.DummyStudentDatabase(self.path)
        database.link_account("pool", "uid", "100000")
        self.mutate("UPDATE students SET program_id = 1 WHERE student_number = 100000")
        with self.assertRaises(demo.DummyStudentDatabaseError):
            database.get_by_uid("pool", "uid")
        self.mutate("INSERT INTO account_links VALUES (?, ?, ?)", ("pool", "dangling", 100006))
        with self.assertRaises(demo.DummyStudentDatabaseError):
            database.get_by_uid("pool", "dangling")
        with self.assertRaises(demo.DummyStudentDatabaseError):
            self.create(resume=True)

    def test_exact_ids_do_not_alias_other_values(self):
        self.create()
        database = demo.DummyStudentDatabase(self.path)

        class CoercingString(str):
            def __int__(self):
                return 100000

        invalid = ("0100000", "000100000", "+100000", "-100000", " 100000", "100000 ", "100000\n",
                   "\u0661\u0660\u0660\u0660\u0660\u0660", "\uff11\uff10\uff10\uff10\uff10\uff10",
                   100000, 100000.0, True, None, b"100000", "99999", "3000001", "100006",
                   "9" * 10000, "100000 OR 1=1", "1e5", "100000.0", "", "100000\x00", CoercingString("100001"))
        for value in invalid:
            with self.subTest(value=repr(value)[:80]):
                self.assertEqual(database.link_account("pool", "uid", value), "not_found")
                self.assertIsNone(database.get_by_uid("pool", "uid"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM account_links"), [(0,)])
        self.assertEqual(database.link_account("pool", "uid", "100000"), "linked")

    def test_identity_validation_rejects_guests_and_never_coerces_values(self):
        database = demo.DummyStudentDatabase(self.path)
        for invalid in (None, True, 123, {}, "", " ", "guest", "Guest", "anonymous", "None", "null",
                        "undefined", " uid", "uid\n", "u\x00id", "u\x7fid", "u\ud800id", "\u200b", "x" * 257):
            for pool, uid in ((invalid, "uid"), ("pool", invalid)):
                with self.subTest(pool=pool, uid=uid), patch.object(demo.sqlite3, "connect") as connect:
                    with self.assertRaises(ValueError):
                        database.link_account(pool, uid, "100000")
                    with self.assertRaises(ValueError):
                        database.get_by_uid(pool, uid)
                    connect.assert_not_called()

    def test_idempotence_profile_changes_and_cross_pool_isolation(self):
        self.create()
        database = demo.DummyStudentDatabase(self.path)
        self.assertIsNone(database.get_by_uid("pool", "100000"))
        self.assertEqual(database.link_account("pool-a", "same-uid", "100000"), "linked")
        self.assertEqual(database.link_account("pool-a", "same-uid", "100000"), "already_linked")
        self.assertEqual(database.link_account("pool-a", "same-uid", "100001"), "conflict")
        self.assertEqual(database.link_account("pool-b", "same-uid", "100000"), "conflict")
        self.assertEqual(database.link_account("pool-a", "other-uid", "100000"), "conflict")
        self.assertEqual(database.link_account("pool-b", "same-uid", "100001"), "linked")
        self.assertEqual(database.link_account("pool-a", "same-uid", "100006"), "not_found")
        self.assertEqual(database.get_by_uid("pool-a", "same-uid")["student_number"], "100000")
        self.assertEqual(database.get_by_uid("pool-b", "same-uid")["student_number"], "100001")
        self.assertIsNone(database.get_by_uid("pool-a", "other-uid"))
        self.assertIsNone(database.get_by_uid("unknown-pool", "same-uid"))
        self.assertEqual(self.query("SELECT COUNT(*) FROM account_links"), [(2,)])

    def test_parameters_are_bound_and_account_links_have_database_constraints(self):
        self.create()
        database = demo.DummyStudentDatabase(self.path)
        uid = "uid');DROP/**/TABLE/**/students;--"
        self.assertEqual(database.link_account("pool'quoted", uid, "100000"), "linked")
        self.assertEqual(database.get_by_uid("pool'quoted", uid)["student_id"], uid)
        self.assertEqual(self.query("SELECT COUNT(*) FROM students"), [(6,)])
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for values in (("other-pool", "other", 100000), ("pool'quoted", uid, 100001), ("pool", "missing", 100006)):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO account_links VALUES (?, ?, ?)", values)
                connection.rollback()

    def test_application_calls_use_indexed_queries_without_roster_scans(self):
        self.create()
        database = demo.DummyStudentDatabase(self.path)
        statements = []
        original_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 5)
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(demo.sqlite3, "connect", side_effect=traced_connect):
            self.assertEqual(database.link_account("pool", "uid", "100000"), "linked")
            self.assertEqual(database.get_by_uid("pool", "uid")["student_number"], "100000")
        combined = "\n".join(statements).upper()
        for forbidden in ("COUNT(", "MIN(", "MAX(", "QUICK_CHECK", "INTEGRITY_CHECK", "FOREIGN_KEY_CHECK"):
            self.assertNotIn(forbidden, combined)
        roster_queries = [sql for sql in statements if sql.startswith("SELECT") and "FROM students" in sql]
        self.assertTrue(roster_queries)
        self.assertTrue(all("WHERE student_number = " in sql for sql in roster_queries))
        plan = self.query(
            "EXPLAIN QUERY PLAN SELECT students.* FROM account_links LEFT JOIN students USING (student_number) "
            "WHERE account_links.pool_id = ? AND account_links.uid = ?", ("pool", "uid")
        )
        self.assertTrue(all("SEARCH" in row[3] and "SCAN" not in row[3] for row in plan), plan)

    def test_thread_race_has_exactly_one_owner(self):
        self.create()
        database = demo.DummyStudentDatabase(self.path)
        barrier = threading.Barrier(8)

        def claim(index):
            barrier.wait(timeout=20)
            return database.link_account(f"pool-{index % 2}", f"uid-{index}", "100000")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        self.assertEqual(results.count("linked"), 1)
        self.assertEqual(results.count("conflict"), 7)
        winner = results.index("linked")
        self.assertEqual(database.link_account(f"pool-{winner % 2}", f"uid-{winner}", "100000"), "already_linked")
        self.assertEqual(self.query("SELECT COUNT(*) FROM account_links"), [(1,)])

    def test_same_account_race_cannot_move_a_binding(self):
        self.create()
        database = demo.DummyStudentDatabase(self.path)
        barrier = threading.Barrier(6)

        def claim(number):
            barrier.wait(timeout=20)
            return database.link_account("pool", "same-uid", str(number))

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(claim, range(100000, 100006)))
        self.assertEqual(results.count("linked"), 1)
        self.assertEqual(results.count("conflict"), 5)
        self.assertEqual(database.get_by_uid("pool", "same-uid")["student_number"], str(100000 + results.index("linked")))

    def test_process_race_has_exactly_one_owner(self):
        self.create()
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(2), context.Queue()
        workers = [context.Process(target=_process_claim, args=(str(self.path), f"uid-{index}", barrier, results))
                   for index in range(2)]
        try:
            for worker in workers:
                worker.start()
            outcomes = [results.get(timeout=30) for _ in workers]
            self.assertEqual(sorted(result for _, result in outcomes), ["conflict", "linked"])
        finally:
            for worker in workers:
                if worker.pid is not None:
                    worker.join(timeout=10)
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=10)
            results.close()
            results.join_thread()
        self.assertTrue(all(worker.exitcode == 0 for worker in workers))
        self.assertEqual(self.query("SELECT COUNT(*) FROM account_links"), [(1,)])

    def test_legacy_reads_are_unchanged_and_writes_require_explicit_upgrade(self):
        self.create_legacy()
        self.mutate("INSERT INTO account_links VALUES (?, ?, ?)", ("pool", "uid", 100000))
        database = demo.DummyStudentDatabase(self.path)
        before = self.path.read_bytes()
        record = database.get_by_uid("pool", "uid")
        self.assertEqual(record["program"], "Demo Computing")
        self.assertEqual(record["section"], "Demo A")
        self.assertEqual([item["subject_code"] for item in record["enrolled_subjects"]], ["DEMO101", "DEMO102", "DEMO103"])
        self.assertTrue(record["is_demo"])
        self.assertNotIn("subject_catalog_source", record)
        with self.assertRaisesRegex(demo.DummyStudentDatabaseError, "update-subjects"):
            database.link_account("pool", "new-uid", "100001")
        with self.assertRaisesRegex(demo.DummyStudentDatabaseError, "update-subjects"):
            self.create(resume=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.query("PRAGMA user_version"), [(1,)])

    def test_upgrade_preserves_protected_values_links_and_table_roots(self):
        self.create_legacy()
        self.mutate("INSERT INTO account_links VALUES (?, ?, ?)", ("pool", "uid", 100004))
        protected_query = "SELECT student_number, section, grade_1, grade_2, grade_3, total_paid FROM students ORDER BY student_number"
        roots_query = "SELECT name, rootpage FROM sqlite_master WHERE name IN ('students', 'account_links') ORDER BY name"
        protected, roots = self.query(protected_query), self.query(roots_query)
        links = self.query("SELECT * FROM account_links")
        backup = self.directory / "before # subjects.sqlite3"
        result = demo.update_subject_catalog(self.path, backup_path=backup)
        self.assertEqual(result, {"row_count": 6, "from_version": 1, "to_version": 2,
                                  "backup_path": str(backup), "changed": True, "linked_accounts": 1})
        self.assertEqual(self.query(protected_query), protected)
        self.assertEqual(self.query(roots_query), roots)
        self.assertEqual(self.query("SELECT * FROM account_links"), links)
        self.assertEqual(self.query("SELECT program_id, year_level, semester FROM students ORDER BY student_number"),
                         [(0, 1, 1), (0, 1, 2), (0, 2, 1), (0, 2, 2), (0, 3, 1), (0, 3, 2)])
        self.assertEqual(demo.DummyStudentDatabase(self.path).get_by_uid("pool", "uid")["program_code"], "BSIT")
        self.assertTrue(demo.DummyStudentDatabase(backup).get_by_uid("pool", "uid")["program"].startswith("Demo "))
        if os.name != "nt":
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        existing = {path for path in self.directory.iterdir() if path.suffix == ".sqlite3"}
        repeated = demo.update_subject_catalog(self.path, backup_path=self.directory / "unnecessary.sqlite3")
        self.assertEqual(repeated, {"row_count": 6, "from_version": 2, "to_version": 2,
                                    "backup_path": None, "changed": False, "linked_accounts": 1})
        self.assertEqual({path for path in self.directory.iterdir() if path.suffix == ".sqlite3"}, existing)

    def test_partial_legacy_prefix_upgrades_without_becoming_complete(self):
        self.create_legacy(row_count=2)
        result = demo.update_subject_catalog(self.path)
        self.assertTrue(result["changed"])
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(self.query("SELECT schema_version, start_id, end_id, row_count, complete FROM demo_metadata"),
                         [(2, 100000, 100005, 2, 0)])
        self.assertTrue(Path(result["backup_path"]).is_file())
        self.assertEqual(Path(result["backup_path"]).parent, self.directory)
        with self.assertRaises(demo.DummyStudentDatabaseError):
            demo.DummyStudentDatabase(self.path).get_by_uid("pool", "uid")
        self.assertTrue(self.create(resume=True)["complete"])

    def test_upgrade_refuses_unknown_sources_existing_backups_and_self_backup(self):
        backup = self.directory / "backup.sqlite3"
        with self.assertRaises(demo.DummyStudentDatabaseError):
            demo.update_subject_catalog(self.path, backup_path=backup)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.path.write_bytes(b"unrecognized file")
        with self.assertRaises(demo.DummyStudentDatabaseError):
            demo.update_subject_catalog(self.path, backup_path=backup)
        self.assertFalse(backup.exists())
        self.assertEqual(self.path.read_bytes(), b"unrecognized file")
        self.path = self.directory / "valid-legacy.sqlite3"
        self.create_legacy()
        backup.write_bytes(b"keep existing backup")
        before = self.path.read_bytes()
        for target in (backup, self.path):
            with self.assertRaises(FileExistsError):
                demo.update_subject_catalog(self.path, backup_path=target)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(backup.read_bytes(), b"keep existing backup")

    def test_upgrade_failure_rolls_back_rows_and_ddl_without_deleting_backup(self):
        self.create_legacy()
        before = {table: self.query(f"SELECT * FROM {table}")
                  for table in ("students", "account_links", "demo_metadata", "demo_courses")}
        backup = self.directory / "retained.sqlite3"
        with patch.dict(demo._SCHEMA, {"demo_courses": "CREATE TABLE invalid ("}), \
                self.assertRaises(demo.DummyStudentDatabaseError):
            demo.update_subject_catalog(self.path, backup_path=backup)
        self.assertEqual(self.query("PRAGMA user_version"), [(1,)])
        for table, rows in before.items():
            self.assertEqual(self.query(f"SELECT * FROM {table}"), rows)
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("SELECT row_count, complete FROM demo_metadata").fetchone(), (6, 1))
        self.assertTrue(demo.update_subject_catalog(self.path)["changed"])

    def test_sqlite_backup_includes_wal_and_claims_added_after_snapshot_survive(self):
        self.create_legacy()
        backup = self.directory / "wal-snapshot.sqlite3"
        original_connect = sqlite3.connect
        source_uri = self.path.as_uri() + "?mode=rw"

        class ConcurrentClaimConnection(sqlite3.Connection):
            def backup(self, target, **kwargs):
                super().backup(target, **kwargs)
                with closing(original_connect(source_uri, uri=True)) as writer:
                    writer.execute("INSERT INTO account_links VALUES (?, ?, ?)", ("pool", "after-snapshot", 100001))
                    writer.commit()

        def connect(*args, **kwargs):
            if args[0] == source_uri:
                kwargs["factory"] = ConcurrentClaimConnection
            return original_connect(*args, **kwargs)

        with closing(original_connect(source_uri, uri=True)) as keeper:
            keeper.execute("PRAGMA wal_autocheckpoint = 0")
            keeper.execute("INSERT INTO account_links VALUES (?, ?, ?)", ("pool", "in-wal", 100000))
            keeper.commit()
            with patch.object(demo.sqlite3, "connect", side_effect=connect):
                result = demo.update_subject_catalog(self.path, backup_path=backup)
        self.assertEqual(result["linked_accounts"], 2)
        current, saved = demo.DummyStudentDatabase(self.path), demo.DummyStudentDatabase(backup)
        self.assertIsNotNone(saved.get_by_uid("pool", "in-wal"))
        self.assertIsNone(saved.get_by_uid("pool", "after-snapshot"))
        for uid in ("in-wal", "after-snapshot"):
            self.assertIsNotNone(current.get_by_uid("pool", uid))

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "create_dummy_students.py"),
                               "--db", str(self.path), *arguments], cwd=self.directory,
                              capture_output=True, text=True, timeout=30, check=False)

    def test_cli_help_is_offline_and_writes_nothing(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SYNTHETIC-ONLY", result.stdout)
        self.assertIn("3000000", result.stdout)
        self.assertIn("STUDENT_RECORDS_BACKEND=sqlite_demo", result.stdout)
        self.assertIn("DUMMY_STUDENT_DB_PATH", result.stdout)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_cli_create_progress_no_clobber_and_resume(self):
        arguments = ("--start-id", "100000", "--end-id", "100002", "--batch-size", "2")
        original_environment = {key: os.environ.get(key) for key in ("STUDENT_RECORDS_BACKEND", "DUMMY_STUDENT_DB_PATH")}
        result = self.run_cli(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ("SYNTHETIC-ONLY", "0/3", "2/3", "3/3", "STUDENT_RECORDS_BACKEND=sqlite_demo",
                     f"DUMMY_STUDENT_DB_PATH={self.path}", "not enabled by this command"):
            self.assertIn(text, result.stdout)
        before = self.path.read_bytes()
        result = self.run_cli(*arguments)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        result = self.run_cli(*arguments, "--resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3/3", result.stdout)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual({key: os.environ.get(key) for key in original_environment}, original_environment)

    def test_cli_rejects_invalid_ranges_and_missing_resume_without_writes(self):
        for arguments in (("--start-id", "99999"), ("--batch-size", "0"), ("--end-id", "3000001"), ("--resume",)):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_cli_upgrade_backup_and_repeat_noop(self):
        self.create_legacy()
        backup = self.directory / "cli-backup.sqlite3"
        result = self.run_cli("--update-subjects", "--backup", str(backup))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("v1 -> v2", result.stdout)
        self.assertIn(str(backup), result.stdout)
        self.assertIn("remain synthetic", result.stdout)
        before = set(self.directory.iterdir())
        result = self.run_cli("--update-subjects")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already current", result.stdout)
        self.assertEqual(set(self.directory.iterdir()), before)

    def test_cli_upgrade_modes_are_validated_before_files(self):
        for arguments in (("--backup", str(self.directory / "backup.sqlite3")),
                          ("--update-subjects", "--resume"),
                          ("--update-subjects", "--end-id", "100005")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
