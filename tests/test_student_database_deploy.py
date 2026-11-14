"""Offline SSH/SCP deployment primitives; all fixtures contain at most six students."""

import errno
import hashlib
import inspect
import multiprocessing
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag import dummy_students as demo
from rag import student_database_deploy as deploy

SCOPE = {"pool_id": "pool-a", "expected_start": 100000, "expected_end": 100005}


def _process_install(destination, source, barrier, results):
    try:
        barrier.wait(timeout=20)
        result = deploy.provision_database(destination, source=source, **SCOPE)
        results.put(("ok", result["status"]))
    except (demo.DummyStudentDatabaseError, ValueError, OSError, threading.BrokenBarrierError) as exc:
        results.put(("error", type(exc).__name__))


class StudentDatabaseDeployTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sc_student_deploy_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.parent = self.root / "private host data"
        self.parent.mkdir(mode=0o700)
        self.source = self.root / "source # students.sqlite3"
        self.export = self.root / "portable # students.sqlite3"
        self.target = self.parent / "students # host.sqlite3"

    def create_source(self):
        demo.create_database(self.source, start_id=100000, end_id=100005, batch_size=2)
        demo.DummyStudentDatabase(self.source).link_account("pool-a", "owner", "100000")

    def create_export(self):
        self.create_source()
        return demo.snapshot_database(self.source, self.export, **SCOPE)

    def install(self, **kwargs):
        return deploy.provision_database(self.target, source=self.export, **SCOPE, **kwargs)

    def query(self, path, sql, parameters=()):
        with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute(sql, parameters).fetchall()

    def assert_no_staging(self):
        self.assertFalse(any(path.name.startswith(".student-db-stage-") for path in self.parent.iterdir()))

    def symlink(self, target, link, *, directory=False):
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {type(exc).__name__}")

    def incomplete(self, path):
        def stop(done, total):
            if done == 2:
                raise RuntimeError("fixture interruption")

        with self.assertRaises(RuntimeError):
            demo.create_database(path, start_id=100000, end_id=100005, batch_size=2, progress=stop)

    def legacy(self, path):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for sql in demo._LEGACY_SCHEMA.values():
                connection.execute(sql)
            connection.execute("PRAGMA application_id = 1396917325")
            connection.execute("PRAGMA user_version = 1")
            connection.execute("INSERT INTO demo_metadata VALUES (1, 1, 'sqlite_demo', 100000, 100005, 6, 1)")
            connection.executemany("INSERT INTO demo_courses VALUES (?, ?, ?, ?, ?, ?, ?, ?)", demo._LEGACY_COURSES)
            connection.executemany("INSERT INTO students VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (demo._student_values(number, version=1) for number in range(100000, 100006)))
            connection.commit()

    def test_signatures_require_pool_and_default_to_the_entire_inclusive_range(self):
        for function in (demo.inspect_database, demo.snapshot_database, deploy.provision_database):
            parameters = inspect.signature(function).parameters
            self.assertIs(parameters["pool_id"].default, inspect.Parameter.empty)
            self.assertEqual(parameters["expected_start"].default, 100000)
            self.assertEqual(parameters["expected_end"].default, 3000000)
            self.assertEqual(parameters["expected_end"].default - parameters["expected_start"].default + 1, 2900001)
        self.assertEqual(set(inspect.signature(deploy.provision_database).parameters),
                         {"destination", "source", "pool_id", "expected_start", "expected_end", "expected_sha256", "expected_size"})

    def test_exact_manifest_and_write_probe_without_record_or_journal_changes(self):
        self.create_source()
        before = self.source.read_bytes()
        statements = []
        original_connect = sqlite3.connect

        def traced(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(demo.sqlite3, "connect", side_effect=traced):
            manifest = demo.inspect_database(self.source, **SCOPE, writable=True)
        self.assertEqual(manifest, {"path": str(self.source), "schema_version": 2,
                                    "start_id": 100000, "end_id": 100005, "row_count": 6, "linked_accounts": 1})
        self.assertEqual(self.source.read_bytes(), before)
        self.assertIn("PRAGMA integrity_check", statements)
        self.assertIn("BEGIN IMMEDIATE", statements)
        self.assertIn("ROLLBACK", statements)
        self.assertNotIn("PRAGMA JOURNAL_MODE", "\n".join(statements).upper())
        self.assertEqual(self.query(self.source, "PRAGMA journal_mode"), [("wal",)])
        self.assertNotIn("owner", str(manifest))
        self.assertNotIn("pool-a", str(manifest))

    def test_full_range_and_equal_count_different_range_are_rejected(self):
        self.create_source()
        scopes = ({"pool_id": "pool-a"}, {"pool_id": "pool-a", "expected_start": 100001, "expected_end": 100006})
        for scope in scopes:
            with self.subTest(scope=scope):
                with self.assertRaises(demo.DummyStudentDatabaseError):
                    demo.inspect_database(self.source, **scope)
                with self.assertRaises(demo.DummyStudentDatabaseError):
                    demo.snapshot_database(self.source, self.export, **scope)
                with self.assertRaises(demo.DummyStudentDatabaseError):
                    deploy.provision_database(self.target, source=self.source, **scope)
        self.assertFalse(self.export.exists())
        self.assertFalse(self.target.exists())
        self.assert_no_staging()

    def test_invalid_scope_arguments_fail_before_connecting(self):
        options = [{"pool_id": value} for value in (None, "", "guest", " pool-a", "pool\n", True, 123)]
        options += [{key: value} for key in ("expected_start", "expected_end")
                    for value in (True, False, "100000", 100000.0, None, 99999, 3000001)]
        for changes in options:
            scope = {**SCOPE, **changes}
            with self.subTest(scope=scope), patch.object(demo.sqlite3, "connect") as connect:
                with self.assertRaises(ValueError):
                    demo.inspect_database(self.source, **scope)
                with self.assertRaises(ValueError):
                    demo.snapshot_database(self.source, self.export, **scope)
                with self.assertRaises(ValueError):
                    deploy.provision_database(self.target, **scope)
                connect.assert_not_called()
        with self.assertRaises(ValueError):
            demo.inspect_database(self.source, **SCOPE, writable="yes")

    def test_pool_mismatch_fails_closed_without_identifier_details(self):
        self.create_source()
        for operation in (lambda: demo.inspect_database(self.source, **{**SCOPE, "pool_id": "other-pool"}),
                          lambda: demo.snapshot_database(self.source, self.export, **{**SCOPE, "pool_id": "other-pool"})):
            with self.assertRaises(demo.DummyStudentDatabaseError) as error:
                operation()
            for identifier in ("pool-a", "other-pool", "owner"):
                self.assertNotIn(identifier, str(error.exception))
        demo.DummyStudentDatabase(self.source).link_account("pool-b", "different-owner", "100001")
        with self.assertRaises(demo.DummyStudentDatabaseError):
            demo.inspect_database(self.source, **SCOPE)
        self.assertFalse(self.export.exists())

    def test_export_includes_wal_and_returns_hash_of_portable_closed_bytes(self):
        self.create_source()
        with closing(sqlite3.connect(self.source)) as keeper:
            keeper.execute("PRAGMA wal_autocheckpoint = 0")
            keeper.execute("INSERT INTO account_links VALUES (?, ?, ?)", ("pool-a", "wal-owner", 100001))
            keeper.commit()
            wal = Path(str(self.source) + "-wal")
            before, before_wal = self.source.read_bytes(), wal.read_bytes()
            self.assertTrue(before_wal)
            result = demo.snapshot_database(self.source, self.export, **SCOPE)
            self.assertEqual(self.source.read_bytes(), before)
            self.assertEqual(wal.read_bytes(), before_wal)
            self.assertEqual(keeper.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(result["path"], str(self.export))
        self.assertEqual(result["linked_accounts"], 2)
        self.assertEqual(result["sha256"], hashlib.sha256(self.export.read_bytes()).hexdigest())
        self.assertEqual(result["size_bytes"], self.export.stat().st_size)
        self.assertEqual(self.query(self.export, "PRAGMA journal_mode"), [("delete",)])
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.export) + suffix).exists())
        transported = self.root / "transported.sqlite3"
        shutil.copyfile(self.export, transported)
        self.assertEqual(demo.DummyStudentDatabase(transported).get_by_uid("pool-a", "wal-owner")["student_number"], "100001")

    def test_export_pins_account_links_while_new_claims_continue(self):
        self.create_source()
        original_connect = sqlite3.connect
        source_uri, writable_uri = self.source.as_uri() + "?mode=ro", self.source.as_uri() + "?mode=rw"

        class ConcurrentConnection(sqlite3.Connection):
            def backup(self, target, **kwargs):
                super().backup(target, **kwargs)
                with closing(original_connect(writable_uri, uri=True)) as writer:
                    writer.execute("INSERT INTO account_links VALUES (?, ?, ?)", ("pool-a", "later-owner", 100001))
                    writer.commit()

        def connect(*args, **kwargs):
            if args[0] == source_uri:
                kwargs["factory"] = ConcurrentConnection
            return original_connect(*args, **kwargs)

        with patch.object(demo.sqlite3, "connect", side_effect=connect):
            result = demo.snapshot_database(self.source, self.export, **SCOPE)
        self.assertEqual(result["linked_accounts"], 1)
        self.assertEqual(demo.inspect_database(self.source, **SCOPE)["linked_accounts"], 2)
        self.assertIsNone(demo.DummyStudentDatabase(self.export).get_by_uid("pool-a", "later-owner"))

    def test_read_only_source_bytes_ownership_and_mode_are_preserved(self):
        self.create_export()
        self.export.chmod(0o444)
        self.addCleanup(self.export.chmod, 0o600)
        before, mode = self.export.read_bytes(), self.export.stat()
        result = demo.snapshot_database(self.export, self.root / "second.sqlite3", **SCOPE)
        after = self.export.stat()
        self.assertEqual(result["row_count"], 6)
        self.assertEqual(self.export.read_bytes(), before)
        self.assertEqual((after.st_mode, after.st_uid, after.st_gid), (mode.st_mode, mode.st_uid, mode.st_gid))

    def test_missing_corrupt_partial_legacy_and_damaged_current_sources_are_rejected(self):
        partial, legacy, corrupt = (self.root / name for name in ("partial.sqlite3", "legacy.sqlite3", "corrupt.sqlite3"))
        self.incomplete(partial)
        self.legacy(legacy)
        corrupt.write_bytes(b"not SQLite")
        for source in (self.source, partial, legacy, corrupt):
            with self.subTest(source=source.name):
                with self.assertRaises(demo.DummyStudentDatabaseError):
                    demo.inspect_database(source, **SCOPE)
                with self.assertRaises(demo.DummyStudentDatabaseError):
                    demo.snapshot_database(source, self.export, **SCOPE)
                self.assertFalse(self.export.exists())
        with self.assertRaisesRegex(demo.DummyStudentDatabaseError, "update-subjects"):
            demo.inspect_database(legacy, **SCOPE)
        self.create_source()
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("DELETE FROM students WHERE student_number = 100005")
            connection.commit()
        with self.assertRaises(demo.DummyStudentDatabaseError):
            demo.inspect_database(self.source, **SCOPE)

    def test_export_no_clobber_same_file_and_no_parent_creation(self):
        self.create_source()
        self.export.write_bytes(b"preserve existing output")
        for target in (self.source, self.export):
            with self.assertRaises(FileExistsError):
                demo.snapshot_database(self.source, target, **SCOPE)
        self.assertEqual(self.export.read_bytes(), b"preserve existing output")
        missing_parent = self.root / "not-prepared"
        with self.assertRaises(demo.DummyStudentDatabaseError):
            demo.snapshot_database(self.source, missing_parent / "snapshot.sqlite3", **SCOPE)
        self.assertFalse(missing_parent.exists())

    def test_signed_install_is_private_and_only_new_target_switches_to_wal(self):
        exported = self.create_export()
        before, mode = self.export.read_bytes(), self.export.stat()
        result = self.install(expected_sha256=exported["sha256"].upper(), expected_size=exported["size_bytes"])
        self.assertEqual(result, {"path": str(self.target), "schema_version": 2, "start_id": 100000,
                                  "end_id": 100005, "row_count": 6, "linked_accounts": 1, "status": "installed"})
        self.assertEqual(self.query(self.target, "PRAGMA journal_mode"), [("wal",)])
        self.assertEqual(self.query(self.export, "PRAGMA journal_mode"), [("delete",)])
        self.assertEqual(self.export.read_bytes(), before)
        self.assertEqual((self.export.stat().st_mode, self.export.stat().st_uid, self.export.stat().st_gid),
                         (mode.st_mode, mode.st_uid, mode.st_gid))
        if os.name != "nt":
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.export.stat().st_mode & 0o777, 0o600)
        self.assert_no_staging()

    def test_existing_host_ignores_even_malicious_source_and_hash_arguments(self):
        self.create_source()
        demo.snapshot_database(self.source, self.target, **SCOPE)
        before = self.target.read_bytes()

        class UntouchablePath:
            def __fspath__(self):
                raise AssertionError("source must not be touched")

        with patch.object(deploy, "snapshot_database", side_effect=AssertionError("no bootstrap")) as snapshot:
            result = deploy.provision_database(self.target, source=UntouchablePath(), **SCOPE,
                                               expected_sha256=object(), expected_size=False)
            self.assertEqual(result["status"], "preserved")
            self.assertEqual(deploy.provision_database(self.target, **SCOPE)["status"], "preserved")
            snapshot.assert_not_called()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.query(self.target, "PRAGMA journal_mode"), [("delete",)])
        self.assert_no_staging()

    def test_invalid_existing_host_never_uses_source_fallback(self):
        self.target.write_bytes(b"preserve invalid host")
        with patch.object(deploy, "snapshot_database") as snapshot:
            with self.assertRaises(demo.DummyStudentDatabaseError):
                self.install()
            snapshot.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"preserve invalid host")
        self.assert_no_staging()

    def test_existing_host_wrong_pool_or_range_is_not_replaced(self):
        self.create_source()
        demo.snapshot_database(self.source, self.target, **SCOPE)
        before = self.target.read_bytes()
        for scope in ({**SCOPE, "pool_id": "wrong-pool"}, {"pool_id": "pool-a"}):
            with self.subTest(scope=scope), patch.object(deploy, "snapshot_database") as snapshot:
                with self.assertRaises(demo.DummyStudentDatabaseError):
                    deploy.provision_database(self.target, source=self.export, **scope)
                snapshot.assert_not_called()
        self.assertEqual(self.target.read_bytes(), before)

    def test_bad_checksum_size_and_argument_types_are_not_published(self):
        exported = self.create_export()
        for options in ({"expected_sha256": "0" * 64}, {"expected_size": exported["size_bytes"] + 1}):
            with self.subTest(options=options), self.assertRaises(demo.DummyStudentDatabaseError):
                self.install(**options)
            self.assertFalse(self.target.exists())
        invalid = [{"expected_sha256": value} for value in ("x", "g" * 64, True, 123, " " + "0" * 64)]
        invalid += [{"expected_size": value} for value in (True, False, 0, -1, "123", 1.5)]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.install(**options)
        self.assertFalse(self.target.exists())
        self.assert_no_staging()

    def test_signed_or_sized_source_rejects_unsigned_wal(self):
        self.create_source()
        with closing(sqlite3.connect(self.source)) as keeper:
            keeper.execute("PRAGMA wal_autocheckpoint = 0")
            keeper.execute("INSERT INTO account_links VALUES (?, ?, ?)", ("pool-a", "unsigned-wal-owner", 100001))
            keeper.commit()
            before = self.source.read_bytes()
            for options in ({"expected_sha256": hashlib.sha256(before).hexdigest()}, {"expected_size": len(before)}):
                with self.subTest(options=options), patch.object(deploy, "snapshot_database") as snapshot:
                    with self.assertRaises(demo.DummyStudentDatabaseError):
                        deploy.provision_database(self.target, source=self.source, **SCOPE, **options)
                    snapshot.assert_not_called()
            self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.target.exists())
        self.assert_no_staging()

    def test_signed_source_changes_are_detected_by_bytes_not_just_timestamps(self):
        exported = self.create_export()
        original_snapshot = deploy.snapshot_database
        original_stamp = demo._file_stamp(self.export.stat())

        def mutate_source(*args, **kwargs):
            demo.DummyStudentDatabase(self.export).link_account("pool-a", "changed-source", "100001")
            return original_snapshot(*args, **kwargs)

        with patch.object(deploy, "snapshot_database", side_effect=mutate_source), \
                patch.object(deploy, "_file_stamp", return_value=original_stamp), \
                self.assertRaises(demo.DummyStudentDatabaseError):
            self.install(expected_sha256=exported["sha256"], expected_size=exported["size_bytes"])
        self.assertFalse(self.target.exists())
        self.assertIsNotNone(demo.DummyStudentDatabase(self.export).get_by_uid("pool-a", "changed-source"))
        self.assert_no_staging()

    def test_old_mtime_is_not_treated_as_a_freshness_policy(self):
        exported = self.create_export()
        os.utime(self.export, (1, 1))
        result = self.install(expected_sha256=exported["sha256"], expected_size=exported["size_bytes"])
        self.assertEqual(result["status"], "installed")

    def test_absent_source_partial_and_legacy_input_never_seed_or_publish(self):
        with self.assertRaises(demo.DummyStudentDatabaseError):
            deploy.provision_database(self.target, **SCOPE)
        partial, legacy = self.root / "partial.sqlite3", self.root / "legacy.sqlite3"
        self.incomplete(partial)
        self.legacy(legacy)
        for source in (self.export, partial, legacy):
            with self.subTest(source=source.name), self.assertRaises(demo.DummyStudentDatabaseError):
                deploy.provision_database(self.target, source=source, **SCOPE)
        self.assertFalse(self.target.exists())
        self.assert_no_staging()

    def test_parent_write_probe_is_required_even_for_inspect_and_existing_host(self):
        self.create_source()
        demo.snapshot_database(self.source, self.target, **SCOPE)
        before = self.target.read_bytes()
        with patch.object(demo.tempfile, "TemporaryDirectory", side_effect=OSError(errno.EROFS, "read-only directory")):
            with self.assertRaises(OSError):
                demo.inspect_database(self.target, **SCOPE, writable=True)
            with self.assertRaises(OSError):
                deploy.provision_database(self.target, **SCOPE)
        self.assertEqual(self.target.read_bytes(), before)

    def test_parent_traversal_is_rejected_before_resolution_or_sqlite(self):
        unsafe = self.parent / "not-created" / ".." / "db.sqlite3"
        with patch.object(demo, "_database_path", side_effect=AssertionError("must not resolve")), \
                patch.object(demo.sqlite3, "connect") as connect:
            for operation in (lambda: demo.inspect_database(unsafe, **SCOPE),
                              lambda: demo.snapshot_database(self.source, unsafe, **SCOPE),
                              lambda: deploy.provision_database(unsafe, **SCOPE)):
                with self.assertRaises(ValueError):
                    operation()
            connect.assert_not_called()
        self.assertFalse((self.parent / "not-created").exists())

    def test_dangling_target_and_parent_symlinks_are_rejected(self):
        unrelated = self.root / "must-not-be-created.sqlite3"
        self.symlink(unrelated, self.target)
        self.create_source()
        for operation in (lambda: demo.inspect_database(self.target, **SCOPE),
                          lambda: demo.snapshot_database(self.source, self.target, **SCOPE),
                          lambda: self.install()):
            with self.assertRaises(demo.DummyStudentDatabaseError):
                operation()
        self.assertTrue(self.target.is_symlink())
        self.assertFalse(unrelated.exists())
        parent_link = self.root / "parent-link"
        self.symlink(self.parent, parent_link, directory=True)
        with self.assertRaises(demo.DummyStudentDatabaseError):
            deploy.provision_database(parent_link / "another.sqlite3", source=self.source, **SCOPE)

    def test_windows_junction_reparse_attribute_is_rejected_without_resolving(self):
        original_status = demo._path_status

        def junction(path):
            if path == self.parent:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_status(path)

        with patch.object(demo, "_path_status", side_effect=junction), patch.object(demo.sqlite3, "connect") as connect:
            with self.assertRaises(demo.DummyStudentDatabaseError):
                deploy.provision_database(self.target, **SCOPE)
            connect.assert_not_called()
        self.assert_no_staging()

    def test_source_symlink_and_symlink_sidecars_are_rejected(self):
        self.create_export()
        linked = self.root / "linked-source.sqlite3"
        self.symlink(self.export, linked)
        with self.assertRaises(demo.DummyStudentDatabaseError):
            deploy.provision_database(self.target, source=linked, **SCOPE)
        sidecar = Path(str(self.export) + "-wal")
        self.symlink(self.root / "missing-recovery", sidecar)
        with self.assertRaises(demo.DummyStudentDatabaseError):
            self.install()
        self.assertTrue(sidecar.is_symlink())
        self.assertFalse(self.target.exists())

    def test_nonregular_target_and_orphan_sidecars_are_preserved(self):
        self.target.mkdir(mode=0o700)
        with self.assertRaises(demo.DummyStudentDatabaseError):
            self.install()
        for index, suffix in enumerate(("-wal", "-shm", "-journal")):
            target = self.parent / f"orphan-{index}.sqlite3"
            sidecar = Path(str(target) + suffix)
            sidecar.write_bytes(b"someone else's recovery state")
            with self.subTest(suffix=suffix), self.assertRaises(demo.DummyStudentDatabaseError):
                deploy.provision_database(target, source=self.source, **SCOPE)
            self.assertEqual(sidecar.read_bytes(), b"someone else's recovery state")
            self.assertFalse(target.exists())
        self.assertTrue(self.target.is_dir())

    def test_interruption_cleans_only_this_invocations_staging(self):
        self.create_export()
        other = self.parent / ".student-db-stage-other"
        other.mkdir(mode=0o700)
        retained = other / "do-not-remove.sqlite3"
        retained.write_bytes(b"other invocation")
        with patch.object(deploy, "snapshot_database", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.install()
        self.assertEqual(retained.read_bytes(), b"other invocation")
        self.assertEqual(list(self.parent.iterdir()), [other])
        self.assertFalse(self.target.exists())

    def test_cleanup_failure_does_not_remove_a_published_database(self):
        self.create_export()
        temporary_directory = tempfile.TemporaryDirectory

        @contextmanager
        def failed_cleanup(*args, **kwargs):
            with temporary_directory(*args, **kwargs) as directory:
                yield directory
            raise OSError("fixture cleanup failure")

        with patch.object(deploy.tempfile, "TemporaryDirectory", side_effect=failed_cleanup), self.assertRaises(OSError):
            self.install()
        self.assertEqual(demo.inspect_database(self.target, **SCOPE)["linked_accounts"], 1)
        self.assertEqual(deploy.provision_database(self.target, **SCOPE)["status"], "preserved")
        self.assert_no_staging()

    def test_failed_hard_link_never_falls_back_to_replacement(self):
        self.create_export()
        with patch.object(deploy.os, "link", side_effect=OSError(errno.EXDEV, "no hard links")), self.assertRaises(OSError):
            self.install()
        self.assertFalse(self.target.exists())
        self.assert_no_staging()

    def test_thread_race_has_one_installer(self):
        self.create_export()
        barrier = threading.Barrier(4)
        original_snapshot = deploy.snapshot_database

        def synchronize(*args, **kwargs):
            barrier.wait(timeout=20)
            return original_snapshot(*args, **kwargs)

        with patch.object(deploy, "snapshot_database", side_effect=synchronize), ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: self.install(), range(4)))
        self.assertEqual([result["status"] for result in results].count("installed"), 1)
        self.assertEqual([result["status"] for result in results].count("preserved"), 3)
        self.assert_no_staging()

    def test_late_import_preserves_new_claims_on_the_winner(self):
        self.create_export()
        slow_source = self.root / "slow.sqlite3"
        shutil.copyfile(self.export, slow_source)
        entered, release = threading.Event(), threading.Event()
        original_snapshot = deploy.snapshot_database

        def synchronize(source, destination, **scope):
            if Path(source) == slow_source:
                entered.set()
                if not release.wait(timeout=20):
                    raise RuntimeError("fixture timed out")
            return original_snapshot(source, destination, **scope)

        with patch.object(deploy, "snapshot_database", side_effect=synchronize), ThreadPoolExecutor(max_workers=1) as workers:
            late = workers.submit(deploy.provision_database, self.target, source=slow_source, **SCOPE)
            try:
                self.assertTrue(entered.wait(timeout=20))
                self.assertEqual(self.install()["status"], "installed")
                demo.DummyStudentDatabase(self.target).link_account("pool-a", "host-only-owner", "100002")
            finally:
                release.set()
            result = late.result(timeout=30)
        self.assertEqual(result["status"], "preserved")
        self.assertEqual(result["linked_accounts"], 2)
        self.assertIsNotNone(demo.DummyStudentDatabase(self.target).get_by_uid("pool-a", "host-only-owner"))
        self.assert_no_staging()

    def test_source_error_cannot_defeat_a_concurrent_valid_winner(self):
        self.create_export()
        failed_source = self.root / "disappearing-source.sqlite3"
        original_snapshot = deploy.snapshot_database

        def winner_then_fail(source, destination, **scope):
            if Path(source) == failed_source:
                self.assertEqual(self.install()["status"], "installed")
                raise OSError("source is unavailable")
            return original_snapshot(source, destination, **scope)

        with patch.object(deploy, "snapshot_database", side_effect=winner_then_fail):
            result = deploy.provision_database(self.target, source=failed_source, **SCOPE)
        self.assertEqual(result["status"], "preserved")
        self.assert_no_staging()

    def test_process_race_has_exactly_one_installer(self):
        self.create_export()
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(2), context.Queue()
        workers = [context.Process(target=_process_install,
                                   args=(str(self.target), str(self.export), barrier, results)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            outcomes = [results.get(timeout=30) for _ in workers]
            self.assertEqual(sorted(outcomes), [("ok", "installed"), ("ok", "preserved")])
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
        self.assertEqual(demo.inspect_database(self.target, **SCOPE)["linked_accounts"], 1)
        self.assert_no_staging()

    @unittest.skipIf(os.name == "nt", "POSIX directory modes are not supported on Windows")
    def test_real_readonly_parent_is_rejected(self):
        if os.geteuid() == 0:
            self.skipTest("Root bypasses ordinary permission bits")
        self.create_source()
        demo.snapshot_database(self.source, self.target, **SCOPE)
        self.parent.chmod(0o500)
        try:
            with self.assertRaises(OSError):
                demo.inspect_database(self.target, **SCOPE, writable=True)
            with self.assertRaises(OSError):
                deploy.provision_database(self.target, **SCOPE)
        finally:
            self.parent.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
