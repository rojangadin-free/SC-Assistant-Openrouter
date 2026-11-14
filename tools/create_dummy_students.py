"""Create a separate synthetic SQLite roster without application or service imports."""

import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    # Also keep direct `python tools/create_dummy_students.py --help` free of pyc writes.
    sys.dont_write_bytecode = True
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from rag.dummy_students import (
        DEFAULT_BATCH_SIZE,
        DEFAULT_END_ID,
        DEFAULT_START_ID,
        DummyStudentDatabaseError,
        create_database,
        update_subject_catalog,
    )

    parser = argparse.ArgumentParser(
        description="Create SYNTHETIC-ONLY dummy students in a separate SQLite file. No accounts or services are provisioned.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="After completion, opt in with STUDENT_RECORDS_BACKEND=sqlite_demo and "
               "DUMMY_STUDENT_DB_PATH=<absolute path>. This command does not set application environment variables.",
    )
    parser.add_argument("--db", type=Path, default=root / "dummy_students.sqlite3", help="Output SQLite file")
    parser.add_argument("--start-id", type=int, default=DEFAULT_START_ID, help="First ID, inclusive (minimum 100000)")
    parser.add_argument("--end-id", type=int, default=DEFAULT_END_ID, help="Last ID, inclusive (maximum 3000000)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Rows per atomic batch (1..100000)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="Resume a matching current-version dummy database")
    mode.add_argument("--update-subjects", action="store_true", help="Back up and upgrade the entire existing v1 subject catalog")
    parser.add_argument("--backup", type=Path, help="Nonexisting backup path for --update-subjects (default: timestamped sibling)")
    args = parser.parse_args(argv)
    if args.backup is not None and not args.update_subjects:
        parser.error("--backup is only valid with --update-subjects.")
    if args.update_subjects and (args.start_id, args.end_id, args.batch_size) != (
        DEFAULT_START_ID, DEFAULT_END_ID, DEFAULT_BATCH_SIZE
    ):
        parser.error("--update-subjects uses the stored range; range and batch-size options are only for generation.")

    print("SYNTHETIC-ONLY demo data. No real student records, accounts, or services are provisioned.", flush=True)
    print("Subject labels and units use the seed catalog; identities, grades, fees, and schedules remain synthetic.", flush=True)

    def report(done, total):
        print(f"Persisted {done:,}/{total:,} synthetic students ({done / total:.1%}).", flush=True)

    try:
        if args.update_subjects:
            print("Validating, backing up, and updating subjects without changing account links or payments.", flush=True)
            result = update_subject_catalog(args.db, backup_path=args.backup)
        else:
            result = create_database(
                args.db, start_id=args.start_id, end_id=args.end_id, batch_size=args.batch_size,
                resume=args.resume, progress=report,
            )
    except KeyboardInterrupt:
        if args.update_subjects:
            print("Subject update interrupted. Any backup was retained; no automatic restore was attempted.", file=sys.stderr)
        else:
            print("Interrupted. Committed batches were retained; rerun with the same range and --resume.", file=sys.stderr)
        return 130
    except (DummyStudentDatabaseError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.update_subjects:
        if result["changed"]:
            print(f"Subject catalog updated: v{result['from_version']} -> v{result['to_version']}; "
                  f"{result['row_count']:,} rows and {result['linked_accounts']:,} account links preserved.")
        else:
            print(f"Subject catalog already current (v{result['to_version']}); no catalog changes.")
        if result["backup_path"]:
            print(f"Backup snapshot retained: {result['backup_path']}")
        database_path = str(args.db.expanduser().resolve())
    else:
        print(f"Complete: {result['row_count']:,} synthetic student rows at {result['path']}")
        database_path = result["path"]
    print("Optional application settings (not enabled by this command):")
    print("STUDENT_RECORDS_BACKEND=sqlite_demo")
    print(f"DUMMY_STUDENT_DB_PATH={database_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
