"""Offline, synthetic-only student records and immutable account claims."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from rag.student_subjects import (
    BSIT_SUBJECTS,
    CATALOG_SOURCE,
    PROGRAM_CODE,
    PROGRAM_NAME,
)

DEFAULT_START_ID = 100_000
DEFAULT_END_ID = 3_000_000
DEFAULT_BATCH_SIZE = 10_000
MAX_BATCH_SIZE = 100_000

_APPLICATION_ID = 0x5343444D
_SCHEMA_VERSION = 2
_LEGACY_PROGRAMS = ("Demo Computing", "Demo Business", "Demo Education")
_LEGACY_COURSES = (
    (1, "DEMO101", "Demo Foundations", 3, "Demo Mon 09:00-12:00", "Demo A", "Demo Instructor A", 4500),
    (2, "DEMO102", "Demo Applied Studies", 3, "Demo Tue 09:00-12:00", "Demo B", "Demo Instructor B", 4500),
    (3, "DEMO103", "Demo Workshop", 2, "Demo Wed 09:00-11:00", "Demo C", "Demo Instructor C", 3000),
)
_COURSES = tuple(
    (year, semester, position, code, name, units)
    for (year, semester), subjects in sorted(BSIT_SUBJECTS.items())
    for position, (code, name, units) in enumerate(subjects, 1)
)
_TOTAL_TUITION = 12000
_MISC_FEE = 1000
_NET_ASSESSMENT = _TOTAL_TUITION + _MISC_FEE

_LEGACY_SCHEMA = {
    "demo_metadata": """CREATE TABLE demo_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        data_source TEXT NOT NULL CHECK (data_source = 'sqlite_demo'),
        start_id INTEGER NOT NULL CHECK (start_id BETWEEN 100000 AND 3000000),
        end_id INTEGER NOT NULL CHECK (end_id BETWEEN start_id AND 3000000),
        row_count INTEGER NOT NULL CHECK (row_count BETWEEN 0 AND end_id - start_id + 1),
        complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
        CHECK (complete = 0 OR row_count = end_id - start_id + 1)
    )""",
    "demo_courses": """CREATE TABLE demo_courses (
        course_id INTEGER PRIMARY KEY,
        subject_code TEXT NOT NULL UNIQUE,
        subject_name TEXT NOT NULL,
        units INTEGER NOT NULL CHECK (units > 0),
        schedule TEXT NOT NULL,
        room TEXT NOT NULL,
        instructor TEXT NOT NULL,
        fee INTEGER NOT NULL CHECK (fee >= 0)
    )""",
    "students": """CREATE TABLE students (
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
    "account_links": """CREATE TABLE account_links (
        pool_id TEXT NOT NULL CHECK (length(pool_id) BETWEEN 1 AND 256),
        uid TEXT NOT NULL CHECK (length(uid) BETWEEN 1 AND 256),
        student_number INTEGER NOT NULL UNIQUE REFERENCES students(student_number),
        PRIMARY KEY (pool_id, uid)
    ) WITHOUT ROWID""",
}
_SCHEMA = {
    **_LEGACY_SCHEMA,
    "demo_metadata": _LEGACY_SCHEMA["demo_metadata"].replace("schema_version = 1", "schema_version = 2"),
    "demo_courses": """CREATE TABLE demo_courses (
        year_level INTEGER NOT NULL CHECK (year_level BETWEEN 1 AND 3),
        semester INTEGER NOT NULL CHECK (semester IN (1, 2)),
        position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 10),
        subject_code TEXT NOT NULL,
        subject_name TEXT NOT NULL,
        units INTEGER NOT NULL CHECK (units > 0),
        PRIMARY KEY (year_level, semester, position),
        UNIQUE (year_level, semester, subject_code)
    ) WITHOUT ROWID""",
}


class DummyStudentDatabaseError(RuntimeError):
    """The selected file cannot safely serve as a dummy student database."""


def _database_path(path: str | os.PathLike) -> Path:
    if not isinstance(path, (str, os.PathLike)) or not os.fspath(path):
        raise ValueError("A database filesystem path is required.")
    return Path(path).expanduser().resolve()


def _create_file(path: Path) -> None:
    # Exclusive creation prevents clobbering. POSIX gets 0600; Windows inherits its directory ACL.
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileExistsError("Database or backup already exists; refusing to overwrite it.") from exc
    os.close(descriptor)


@contextmanager
def _connect(path: Path, *, read_only: bool = False, timeout: float = 5):
    # as_uri quotes Windows drive paths, spaces, #, and URI query characters.
    uri = path.as_uri() + ("?mode=ro" if read_only else "?mode=rw")
    try:
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=timeout)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if not read_only:
                connection.execute("PRAGMA synchronous = FULL")
            yield connection
    except sqlite3.Error as exc:
        raise DummyStudentDatabaseError(
            "Dummy student database is unavailable, corrupt, or incompatible."
        ) from exc


@contextmanager
def _transaction(connection, *, write: bool = False):
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _read_metadata(connection) -> dict:
    rows = connection.execute("SELECT * FROM demo_metadata").fetchall()
    if len(rows) != 1:
        raise DummyStudentDatabaseError("Invalid dummy database metadata.")
    metadata = dict(rows[0])
    integer_fields = ("singleton", "schema_version", "start_id", "end_id", "row_count", "complete")
    if any(type(metadata[field]) is not int for field in integer_fields):
        raise DummyStudentDatabaseError("Invalid dummy database metadata types.")
    start, end, count = metadata["start_id"], metadata["end_id"], metadata["row_count"]
    total = end - start + 1
    if (
        metadata["singleton"] != 1
        or metadata["schema_version"] not in (1, _SCHEMA_VERSION)
        or metadata["data_source"] != "sqlite_demo"
        or not DEFAULT_START_ID <= start <= end <= DEFAULT_END_ID
        or not 0 <= count <= total
        or metadata["complete"] not in (0, 1)
        or metadata["complete"] != int(count == total)
    ):
        raise DummyStudentDatabaseError("Invalid dummy database generation progress.")
    return metadata


def _validate_database(connection, *, require_complete: bool, integrity: bool = False) -> dict:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if (
        connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
        or version not in (1, _SCHEMA_VERSION)
    ):
        raise DummyStudentDatabaseError("Not a compatible dummy student database.")
    objects = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
    ).fetchall()
    actual_schema = {(row["type"], row["name"]): " ".join((row["sql"] or "").split()) for row in objects}
    schema = _LEGACY_SCHEMA if version == 1 else _SCHEMA
    expected_schema = {("table", name): " ".join(sql.split()) for name, sql in schema.items()}
    if actual_schema != expected_schema:
        raise DummyStudentDatabaseError("Unrecognized dummy student database schema.")
    metadata = _read_metadata(connection)
    if metadata["schema_version"] != version:
        raise DummyStudentDatabaseError("Dummy database schema and metadata versions do not match.")
    if require_complete and not metadata["complete"]:
        raise DummyStudentDatabaseError("Dummy student database generation is incomplete; resume it first.")
    course_query = ("SELECT * FROM demo_courses ORDER BY course_id" if version == 1 else
                    "SELECT * FROM demo_courses ORDER BY year_level, semester, position")
    courses = tuple(tuple(row) for row in connection.execute(course_query))
    if courses != (_LEGACY_COURSES if version == 1 else _COURSES):
        raise DummyStudentDatabaseError("Invalid shared demo course definitions.")
    if integrity:
        # Full-roster scans belong to generation/resume, not indexed application lookups.
        count, first, last = connection.execute(
            "SELECT (SELECT COUNT(*) FROM students), "
            "(SELECT MIN(student_number) FROM students), (SELECT MAX(student_number) FROM students)"
        ).fetchone()
        expected_first = metadata["start_id"] if count else None
        expected_last = metadata["start_id"] + count - 1 if count else None
        if count != metadata["row_count"] or first != expected_first or last != expected_last:
            raise DummyStudentDatabaseError("Dummy student rows do not match contiguous generation progress.")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise DummyStudentDatabaseError("Dummy student database integrity check failed.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DummyStudentDatabaseError("Dummy student database contains an invalid account link.")
        invalid_row = connection.execute(
            "SELECT 1 FROM students WHERE "
            "program_id != CASE WHEN ? = 1 THEN (student_number - 100000) % 3 ELSE 0 END OR "
            "year_level != CASE WHEN ? = 1 THEN (student_number - 100000) % 4 + 1 "
            "ELSE ((student_number - 100000) / 2) % 3 + 1 END OR "
            "semester != CASE WHEN ? = 1 THEN ((student_number - 100000) / 4) % 2 + 1 "
            "ELSE (student_number - 100000) % 2 + 1 END OR "
            "section != ((student_number - 100000) / 12) % 3 OR "
            "grade_1 != 100 + 25 * ((student_number - 100000) % 9) OR "
            "grade_2 != 100 + 25 * ((student_number - 100000 + 3) % 9) OR "
            "grade_3 != 100 + 25 * ((student_number - 100000 + 6) % 9) OR "
            "total_paid != 13000 * ((student_number - 100000) % 3) / 2 LIMIT 1",
            (version, version, version),
        ).fetchone()
        if invalid_row is not None:
            raise DummyStudentDatabaseError("Invalid persisted dummy student values for this schema version.")
    return metadata


def _student_values(number: int, *, version: int = _SCHEMA_VERSION) -> tuple:
    offset = number - DEFAULT_START_ID
    return (
        number, offset % 3 if version == 1 else 0,
        offset % 4 + 1 if version == 1 else offset // 2 % 3 + 1,
        offset // 12 % 3, offset // 4 % 2 + 1 if version == 1 else offset % 2 + 1,
        100 + 25 * (offset % 9), 100 + 25 * ((offset + 3) % 9),
        100 + 25 * ((offset + 6) % 9), _NET_ASSESSMENT * (offset % 3) // 2,
    )


def create_database(
    path: str | os.PathLike,
    *,
    start_id: int = DEFAULT_START_ID,
    end_id: int = DEFAULT_END_ID,
    batch_size: int = DEFAULT_BATCH_SIZE,
    resume: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Persist an inclusive synthetic roster; callbacks observe committed counts.

    Existing files are never replaced. Resume requires this exact schema/version,
    range, and contiguous persisted progress. Exceptions (including interrupts)
    roll back the current batch; earlier batches remain available for resume.
    """
    if type(start_id) is not int or type(end_id) is not int:
        raise ValueError("Student range bounds must be integers, not booleans.")
    if not DEFAULT_START_ID <= start_id <= end_id <= DEFAULT_END_ID:
        raise ValueError("Student range must be within 100000..3000000 inclusive.")
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("Batch size must be an integer within 1..100000.")
    if type(resume) is not bool or (progress is not None and not callable(progress)):
        raise ValueError("Resume must be boolean and progress must be callable or None.")
    database_path = _database_path(path)
    if not resume:
        _create_file(database_path)

    with _connect(database_path, timeout=30) as connection:
        if not resume:
            if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise DummyStudentDatabaseError("Could not enable WAL for the dummy database.")
            with _transaction(connection, write=True):
                for sql in _SCHEMA.values():
                    connection.execute(sql)
                connection.execute("PRAGMA application_id = 1396917325")
                connection.execute("PRAGMA user_version = 2")
                connection.execute(
                    "INSERT INTO demo_metadata VALUES (1, 2, 'sqlite_demo', ?, ?, 0, 0)",
                    (start_id, end_id),
                )
                connection.executemany("INSERT INTO demo_courses VALUES (?, ?, ?, ?, ?, ?)", _COURSES)
        with _transaction(connection, write=True):
            metadata = _validate_database(connection, require_complete=False, integrity=True)
            if metadata["schema_version"] != _SCHEMA_VERSION:
                raise DummyStudentDatabaseError("Legacy v1 database: run --update-subjects before --resume.")
            if (metadata["start_id"], metadata["end_id"]) != (start_id, end_id):
                raise DummyStudentDatabaseError("Resume requires the original student ID range.")
        total = end_id - start_id + 1
        if progress is not None:
            progress(metadata["row_count"], total)
        while not metadata["complete"]:
            with _transaction(connection, write=True):
                # Another matching generator may have committed while the callback ran.
                metadata = _read_metadata(connection)
                if metadata["schema_version"] != _SCHEMA_VERSION:
                    raise DummyStudentDatabaseError("Generation requires the current subject catalog version.")
                if (metadata["start_id"], metadata["end_id"]) != (start_id, end_id):
                    raise DummyStudentDatabaseError("Dummy database generation range changed.")
                if not metadata["complete"]:
                    first = start_id + metadata["row_count"]
                    stop = min(first + batch_size, end_id + 1)
                    connection.executemany(
                        "INSERT INTO students VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (_student_values(number) for number in range(first, stop)),
                    )
                    metadata["row_count"] = stop - start_id
                    metadata["complete"] = int(stop == end_id + 1)
                    connection.execute(
                        "UPDATE demo_metadata SET row_count = ?, complete = ? WHERE singleton = 1",
                        (metadata["row_count"], metadata["complete"]),
                    )
            if progress is not None:
                progress(metadata["row_count"], total)
    return {
        "path": str(database_path), "start_id": start_id, "end_id": end_id,
        "row_count": metadata["row_count"], "complete": bool(metadata["complete"]),
    }


def _migration_invariants(connection) -> tuple:
    return tuple(connection.execute(
        "SELECT COUNT(*), SUM(student_number), SUM(section), SUM(grade_1), SUM(grade_2), "
        "SUM(grade_3), SUM(total_paid), (SELECT COUNT(*) FROM account_links) FROM students"
    ).fetchone())


def update_subject_catalog(path: str | os.PathLike, *, backup_path: str | os.PathLike | None = None) -> dict:
    """Explicitly upgrade v1 course assignments, preserving claims and synthetic values.

    A validated, exclusive SQLite backup is made before the write transaction.
    It is a snapshot, not an automatic restore point: later claims remain in the
    original database and must not be discarded by restoring the older snapshot.
    Valid partial v1 generation prefixes can be upgraded and then resumed.
    """
    database_path = _database_path(path)
    with _connect(database_path, timeout=30) as connection:
        with _transaction(connection):
            snapshot = _validate_database(connection, require_complete=False, integrity=True)
            if snapshot["schema_version"] == _SCHEMA_VERSION:
                return {
                    "row_count": snapshot["row_count"], "from_version": _SCHEMA_VERSION,
                    "to_version": _SCHEMA_VERSION, "backup_path": None, "changed": False,
                    "linked_accounts": connection.execute("SELECT COUNT(*) FROM account_links").fetchone()[0],
                }
            if backup_path is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup_file = database_path.with_name(f"{database_path.stem}.before-subjects-v2-{stamp}.sqlite3")
            else:
                backup_file = _database_path(backup_path)
            _create_file(backup_file)
            # The source has only a read transaction; backup under our own write lock can self-block.
            with _connect(backup_file, timeout=30) as backup:
                connection.backup(backup, pages=256)
                with _transaction(backup):
                    copied = _validate_database(backup, require_complete=False, integrity=True)
                    if copied != snapshot:
                        raise DummyStudentDatabaseError("Backup does not match the validated legacy snapshot.")

        with _transaction(connection, write=True):
            metadata = _validate_database(connection, require_complete=False, integrity=True)
            before = _migration_invariants(connection)
            from_version = metadata["schema_version"]
            changed = from_version != _SCHEMA_VERSION
            if changed:
                if (metadata["start_id"], metadata["end_id"]) != (snapshot["start_id"], snapshot["end_id"]):
                    raise DummyStudentDatabaseError("Database generation range changed after the backup.")
                connection.execute(
                    "UPDATE students SET program_id = 0, "
                    "year_level = ((student_number - 100000) / 2) % 3 + 1, "
                    "semester = (student_number - 100000) % 2 + 1"
                )
                connection.execute("DROP TABLE demo_courses")
                connection.execute(_SCHEMA["demo_courses"])
                connection.executemany("INSERT INTO demo_courses VALUES (?, ?, ?, ?, ?, ?)", _COURSES)
                connection.execute("DROP TABLE demo_metadata")
                connection.execute(_SCHEMA["demo_metadata"])
                connection.execute(
                    "INSERT INTO demo_metadata VALUES (1, 2, 'sqlite_demo', ?, ?, ?, ?)",
                    (metadata["start_id"], metadata["end_id"], metadata["row_count"], metadata["complete"]),
                )
                connection.execute("PRAGMA user_version = 2")
                updated = _validate_database(connection, require_complete=False, integrity=True)
                if updated != {**metadata, "schema_version": _SCHEMA_VERSION} or _migration_invariants(connection) != before:
                    raise DummyStudentDatabaseError("Subject update changed protected database invariants.")
        return {
            "row_count": metadata["row_count"], "from_version": from_version,
            "to_version": _SCHEMA_VERSION, "backup_path": str(backup_file), "changed": changed,
            "linked_accounts": before[-1],
        }


def _validate_account(pool_id: str, uid: str) -> None:
    for label, value in (("pool_id", pool_id), ("uid", uid)):
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 256
            or value.casefold() in {"guest", "anonymous", "none", "null", "undefined"}
            or not value.isprintable()
            or any(char.isspace() for char in value)
        ):
            raise ValueError(f"{label} must be a nonempty account identifier, not a guest marker.")


def _canonical_number(value) -> int | None:
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{5,6}", value) is None:
        return None
    number = int(value)
    return number if DEFAULT_START_ID <= number <= DEFAULT_END_ID else None


def _record(row, uid: str, version: int) -> dict:
    if any(type(value) is not int for value in row) or tuple(row) != _student_values(row["student_number"], version=version):
        raise DummyStudentDatabaseError("Invalid persisted dummy student record or account link.")
    if version == 1:
        courses = tuple(course[1:] for course in _LEGACY_COURSES)
    else:
        catalog = BSIT_SUBJECTS[(row["year_level"], row["semester"])]
        units_total = sum(units for _, _, units in catalog)
        fees = [_TOTAL_TUITION * units // units_total for _, _, units in catalog]
        fees[-1] += _TOTAL_TUITION - sum(fees)
        courses = tuple(
            (code, name, units, f"Demo Schedule {position}", f"Demo Room {position}", f"Demo Instructor {position}", fee)
            for position, ((code, name, units), fee) in enumerate(zip(catalog, fees), 1)
        )
    subjects, grades, tuition = [], [], []
    grade_seeds = (row["grade_1"], row["grade_2"], row["grade_3"])
    for position, (code, name, units, schedule, room, instructor, fee) in enumerate(courses):
        subject = {"subject_code": code, "subject_name": name, "units": units}
        grade = grade_seeds[position % 3] / 100
        subjects.append({**subject, "schedule": schedule, "room": room, "instructor": instructor})
        grades.append({
            **subject, "prelim": grade, "midterm": grade, "semi_final": grade,
            "final": grade, "final_grade": grade, "remarks": "Demo Passed",
        })
        tuition.append({"subject_code": code, "subject_name": name, "fee": fee})
    gpa = round(sum(grade["final_grade"] * grade["units"] for grade in grades)
                / sum(grade["units"] for grade in grades), 2)
    paid = row["total_paid"]
    balance = _NET_ASSESSMENT - paid
    record = {
        "student_number": str(row["student_number"]), "student_id": uid,
        "is_demo": True, "data_source": "sqlite_demo",
        "full_name": f"Demo Student {row['student_number']}",
        "program": _LEGACY_PROGRAMS[row["program_id"]] if version == 1 else PROGRAM_NAME,
        "year_level": row["year_level"],
        "section": (f"Demo {chr(65 + row['section'])}" if version == 1 else
                    f"{PROGRAM_CODE}-{row['year_level']}{chr(65 + row['section'])}"),
        "semester": "1st" if row["semester"] == 1 else "2nd", "school_year": "2026-2027",
        "student_status": "Demo Regular", "enrollment_status": "Demo Enrolled",
        "remarks": "Synthetic demo academic standing", "gpa": gpa, "cumulative_gpa": gpa,
        "enrolled_subjects": subjects, "grades": grades, "tuition_breakdown": tuition,
        "total_tuition": sum(item["fee"] for item in tuition),
        "misc_fees": {"Demo Student Services": _MISC_FEE},
        "gross_total": _NET_ASSESSMENT, "scholarship_discount": 0,
        "net_assessment": _NET_ASSESSMENT, "total_paid": paid, "balance": balance,
        "payment_status": "Paid in Full" if not balance else ("Partially Paid" if paid else "Unpaid"),
        "minimum_payment": min(balance, 1500),
    }
    if version == _SCHEMA_VERSION:
        record.update(program_code=PROGRAM_CODE, subject_catalog_source=CATALOG_SOURCE)
    return record


class DummyStudentDatabase:
    """Lazy, per-call connections to an existing, completed synthetic database."""

    def __init__(self, path: str | os.PathLike):
        self.path = _database_path(path)

    def link_account(self, pool_id: str, uid: str, student_number: str) -> str:
        """Claim once; profile edits never move an existing account's binding."""
        _validate_account(pool_id, uid)
        number = _canonical_number(student_number)
        with _connect(self.path) as connection, _transaction(connection, write=True):
            metadata = _validate_database(connection, require_complete=True)
            if metadata["schema_version"] != _SCHEMA_VERSION:
                raise DummyStudentDatabaseError("Legacy v1 database is read-only; run --update-subjects before linking.")
            if number is None or connection.execute(
                "SELECT 1 FROM students WHERE student_number = ?", (number,)
            ).fetchone() is None:
                return "not_found"
            existing = connection.execute(
                "SELECT student_number FROM account_links WHERE pool_id = ? AND uid = ?", (pool_id, uid)
            ).fetchone()
            if existing is not None:
                return "already_linked" if existing[0] == number else "conflict"
            if connection.execute(
                "SELECT 1 FROM account_links WHERE student_number = ?", (number,)
            ).fetchone() is not None:
                return "conflict"
            connection.execute("INSERT INTO account_links VALUES (?, ?, ?)", (pool_id, uid, number))
            return "linked"

    def get_by_uid(self, pool_id: str, uid: str) -> dict | None:
        """Read only through a pool/UID binding, never a school-number or email alias."""
        _validate_account(pool_id, uid)
        with _connect(self.path, read_only=True) as connection, _transaction(connection):
            metadata = _validate_database(connection, require_complete=True)
            row = connection.execute(
                "SELECT students.* FROM account_links LEFT JOIN students USING (student_number) "
                "WHERE account_links.pool_id = ? AND account_links.uid = ?", (pool_id, uid)
            ).fetchone()
            return _record(row, uid, metadata["schema_version"]) if row is not None else None


def _path_status(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_link(status) -> bool:
    return stat.S_ISLNK(status.st_mode) or bool(
        getattr(status, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _recovery_paths(path: Path) -> tuple:
    return tuple(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))


def _deployment_path(path: str | os.PathLike) -> Path:
    """Inspect lexical paths before any resolution; existing application paths are unchanged."""
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise ValueError("A database filesystem path is required.") from exc
    if not isinstance(raw, str) or not raw:
        raise ValueError("A database filesystem path is required.")
    lexical = Path(raw).expanduser()
    if ".." in lexical.parts:
        raise ValueError("Parent traversal is not allowed in deployment paths.")
    candidate = Path(os.path.abspath(lexical))
    for parent in reversed(candidate.parents):
        status = _path_status(parent)
        if status is None or _is_link(status) or not stat.S_ISDIR(status.st_mode):
            raise DummyStudentDatabaseError("Database parents must be existing directories without links or reparse points.")
    status = _path_status(candidate)
    if status is not None and (_is_link(status) or not stat.S_ISREG(status.st_mode)):
        raise DummyStudentDatabaseError("Database paths must be regular files, not links or reparse points.")
    for recovery_path in _recovery_paths(candidate):
        recovery = _path_status(recovery_path)
        if recovery is None:
            continue
        if _is_link(recovery) or not stat.S_ISREG(recovery.st_mode):
            raise DummyStudentDatabaseError("Unsafe SQLite recovery file; no recovery state was removed.")
        if status is None:
            # A concurrent winner may publish its database before creating its WAL.
            status = _path_status(candidate)
            if status is None:
                raise DummyStudentDatabaseError("Orphan SQLite recovery files exist; no recovery state was removed.")
            if _is_link(status) or not stat.S_ISREG(status.st_mode):
                raise DummyStudentDatabaseError("Unsafe concurrent database destination.")
    return candidate


def _file_stamp(status) -> tuple | None:
    if status is None:
        return None
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


@contextmanager
def _read_regular(path: Path):
    path = _deployment_path(path)
    before = _path_status(path)
    if before is None:
        raise DummyStudentDatabaseError("An existing regular database file is required.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise DummyStudentDatabaseError("Database file changed while it was being opened.")
        current = _path_status(_deployment_path(path))
        if current is None or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise DummyStudentDatabaseError("Database file was replaced while being opened.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
        current = _path_status(_deployment_path(path))
        if current is None or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise DummyStudentDatabaseError("Database file was replaced while being read.")
    finally:
        os.close(descriptor)


def _file_digest(path: Path) -> dict:
    digest, size = hashlib.sha256(), 0
    with _read_regular(path) as stream:
        before = _file_stamp(os.fstat(stream.fileno()))
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        if before != _file_stamp(os.fstat(stream.fileno())):
            raise DummyStudentDatabaseError("Database bytes changed during checksum verification.")
    return {"sha256": digest.hexdigest(), "size_bytes": size}


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    # Directory fsync is not exposed portably by Windows' os.open/os.fsync.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _deployment_scope(pool_id: str, expected_start: int, expected_end: int) -> None:
    _validate_account(pool_id, "deployment-check")
    if (type(expected_start) is not int or type(expected_end) is not int
            or not DEFAULT_START_ID <= expected_start <= expected_end <= DEFAULT_END_ID):
        raise ValueError("Expected deployment bounds must be integers within 100000..3000000.")


def _deployment_manifest(connection, path: Path, pool_id: str, expected_start: int, expected_end: int) -> dict:
    metadata = _validate_database(connection, require_complete=False, integrity=True)
    if metadata["schema_version"] != _SCHEMA_VERSION:
        raise DummyStudentDatabaseError("Legacy database: run --update-subjects before deployment.")
    if not metadata["complete"]:
        raise DummyStudentDatabaseError("Only a completed student database can be deployed.")
    if ((metadata["start_id"], metadata["end_id"]) != (expected_start, expected_end)
            or metadata["row_count"] != expected_end - expected_start + 1):
        raise DummyStudentDatabaseError("Student database range does not match the expected deployment scope.")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise DummyStudentDatabaseError("Student database index integrity check failed.")
    if connection.execute("SELECT 1 FROM account_links WHERE pool_id != ? LIMIT 1", (pool_id,)).fetchone() is not None:
        raise DummyStudentDatabaseError("Student database account links do not match the expected pool.")
    return {
        "path": str(path), "schema_version": metadata["schema_version"],
        "start_id": metadata["start_id"], "end_id": metadata["end_id"], "row_count": metadata["row_count"],
        "linked_accounts": connection.execute("SELECT COUNT(*) FROM account_links").fetchone()[0],
    }


def inspect_database(
    path: str | os.PathLike, *, pool_id: str, expected_start: int = DEFAULT_START_ID,
    expected_end: int = DEFAULT_END_ID, writable: bool = False,
) -> dict:
    """Fully validate explicit deployment scope without exposing account identifiers."""
    _deployment_scope(pool_id, expected_start, expected_end)
    if type(writable) is not bool:
        raise ValueError("Writable must be boolean.")
    database_path = _deployment_path(path)
    probe = tempfile.TemporaryDirectory(prefix=".student-db-stage-", dir=database_path.parent) if writable else nullcontext()
    with probe, _read_regular(database_path), _connect(database_path, read_only=not writable) as connection:
        with _transaction(connection):
            manifest = _deployment_manifest(connection, database_path, pool_id, expected_start, expected_end)
        if writable:
            # Keep the write lock short: full validation used a read transaction.
            try:
                connection.execute("BEGIN IMMEDIATE")
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
    return manifest


def snapshot_database(
    source: str | os.PathLike, destination: str | os.PathLike, *, pool_id: str,
    expected_start: int = DEFAULT_START_ID, expected_end: int = DEFAULT_END_ID,
) -> dict:
    """Export a point-in-time read snapshot, not a claim that the source is newest.

    Only the exclusive new copy is checkpointed and changed to DELETE journaling.
    Its returned digest describes the closed, self-contained transport file.
    An interrupted export may retain its exclusively created partial destination.
    """
    _deployment_scope(pool_id, expected_start, expected_end)
    source_path, destination_path = _deployment_path(source), _deployment_path(destination)
    if _path_status(destination_path) is not None:
        raise FileExistsError("Snapshot destination already exists; refusing to overwrite it.")
    with _read_regular(source_path), _connect(source_path, read_only=True, timeout=30) as connection, _transaction(connection):
        original = _deployment_manifest(connection, source_path, pool_id, expected_start, expected_end)
        _deployment_path(destination_path)
        _create_file(destination_path)
        with _connect(destination_path, timeout=30) as snapshot:
            connection.backup(snapshot, pages=256)
            with _transaction(snapshot):
                manifest = _deployment_manifest(snapshot, destination_path, pool_id, expected_start, expected_end)
                if manifest != {**original, "path": str(destination_path)}:
                    raise DummyStudentDatabaseError("Export does not match the validated read snapshot.")
            if snapshot.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise DummyStudentDatabaseError("Could not checkpoint the private snapshot.")
            if snapshot.execute("PRAGMA journal_mode = DELETE").fetchone()[0] != "delete":
                raise DummyStudentDatabaseError("Could not make the private snapshot self-contained.")
    if any(_path_status(path) is not None for path in _recovery_paths(destination_path)):
        raise DummyStudentDatabaseError("Exported snapshot still has SQLite recovery files.")
    _fsync_file(destination_path)
    _fsync_directory(destination_path.parent)
    return {**manifest, **_file_digest(destination_path)}
