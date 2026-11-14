"""Offline snapshot installation; SSH/SCP transport and directory preparation are external."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from rag.dummy_students import (
    DEFAULT_END_ID,
    DEFAULT_START_ID,
    DummyStudentDatabaseError,
    _connect,
    _deployment_path,
    _deployment_scope,
    _file_digest,
    _file_stamp,
    _fsync_directory,
    _fsync_file,
    _path_status,
    _read_regular,
    _recovery_paths,
    inspect_database,
    snapshot_database,
)


def _existing_database(destination: Path, scope: dict) -> dict | None:
    _deployment_path(destination)
    if _path_status(destination) is None:
        return None
    return {**inspect_database(destination, **scope, writable=True), "status": "preserved"}


def _require_self_contained(path: Path) -> None:
    _deployment_path(path)
    if any(_path_status(sidecar) is not None for sidecar in _recovery_paths(path)):
        raise DummyStudentDatabaseError("Checksum-constrained snapshots must not have SQLite recovery sidecars.")


def _verified_copy(source: Path, destination: Path, expected_sha256: str | None, expected_size: int | None) -> tuple:
    _require_self_contained(source)
    stamp = _file_stamp(_path_status(source))
    if stamp is None:
        raise DummyStudentDatabaseError("An explicit source snapshot is required for first installation.")
    if expected_size is not None and stamp[2] != expected_size:
        raise DummyStudentDatabaseError("Snapshot size does not match the expected size.")
    fingerprint = _file_digest(source)
    if expected_sha256 is not None and fingerprint["sha256"] != expected_sha256:
        raise DummyStudentDatabaseError("Snapshot checksum does not match the expected checksum.")
    digest, size = hashlib.sha256(), 0
    with _read_regular(source) as incoming:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as outgoing:
            while True:
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                outgoing.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    if {"sha256": digest.hexdigest(), "size_bytes": size} != fingerprint:
        raise DummyStudentDatabaseError("Source snapshot changed while being copied.")
    _check_source_stable(source, fingerprint, stamp)
    return fingerprint, stamp


def _check_source_stable(source: Path, fingerprint: dict, stamp: tuple) -> None:
    _require_self_contained(source)
    # Timestamps are change detectors only; the digest is not a freshness guarantee either.
    if _file_stamp(_path_status(source)) != stamp or _file_digest(source) != fingerprint:
        raise DummyStudentDatabaseError("Source snapshot changed during verification.")


def _publish_no_replace(candidate: Path, destination: Path) -> None:
    _deployment_path(candidate)
    _deployment_path(destination)
    if os.link not in os.supports_dir_fd:
        os.link(candidate, destination, follow_symlinks=False)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(destination.parent, flags)
    try:
        stage_descriptor = os.open(candidate.parent, flags)
        try:
            os.link(candidate.name, destination.name, src_dir_fd=stage_descriptor,
                    dst_dir_fd=parent_descriptor, follow_symlinks=False)
            os.fsync(parent_descriptor)
        finally:
            os.close(stage_descriptor)
    finally:
        os.close(parent_descriptor)


def provision_database(
    destination: str | os.PathLike, *, source: str | os.PathLike | None = None, pool_id: str,
    expected_start: int = DEFAULT_START_ID, expected_end: int = DEFAULT_END_ID,
    expected_sha256: str | None = None, expected_size: int | None = None,
) -> dict:
    """Preserve host authority forever; import an explicit snapshot only when absent.

    A valid existing destination wins before source/checksum arguments are touched.
    No transport, seeding, migration, merging, or persistent lock is performed here.
    """
    _deployment_scope(pool_id, expected_start, expected_end)
    scope = {"pool_id": pool_id, "expected_start": expected_start, "expected_end": expected_end}
    target = _deployment_path(destination)
    existing = _existing_database(target, scope)
    if existing is not None:
        return existing
    with tempfile.TemporaryDirectory(prefix=".student-db-stage-", dir=target.parent) as temporary:
        existing = _existing_database(target, scope)
        if existing is not None:
            return existing
        candidate = Path(temporary) / "ready.sqlite3"
        try:
            if source is None:
                raise DummyStudentDatabaseError("An explicit source snapshot is required for first installation.")
            if expected_sha256 is not None:
                if type(expected_sha256) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None:
                    raise ValueError("Expected SHA-256 must contain exactly 64 hexadecimal characters.")
                expected_sha256 = expected_sha256.lower()
            if expected_size is not None and (type(expected_size) is not int or expected_size <= 0):
                raise ValueError("Expected size must be a positive integer.")
            source_path = _deployment_path(source)
            if expected_sha256 is not None or expected_size is not None:
                raw_copy = Path(temporary) / "verified-source.sqlite3"
                fingerprint, stamp = _verified_copy(source_path, raw_copy, expected_sha256, expected_size)
                snapshot_database(raw_copy, candidate, **scope)
                _check_source_stable(source_path, fingerprint, stamp)
            else:
                snapshot_database(source_path, candidate, **scope)
            # Only a new private candidate changes journal mode; no connection survives publication.
            with _connect(candidate) as connection:
                if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                    raise DummyStudentDatabaseError("Could not enable WAL on the new database.")
                if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise DummyStudentDatabaseError("Could not checkpoint the new database.")
            if any(_path_status(path) is not None for path in _recovery_paths(candidate)):
                raise DummyStudentDatabaseError("The new candidate still requires SQLite recovery files.")
            _fsync_file(candidate)
        except Exception:
            existing = _existing_database(target, scope)
            if existing is not None:
                return existing
            raise
        existing = _existing_database(target, scope)
        if existing is not None:
            return existing
        try:
            _publish_no_replace(candidate, target)
        except FileExistsError:
            existing = _existing_database(target, scope)
            if existing is None:
                raise DummyStudentDatabaseError("Concurrent destination disappeared; nothing was replaced.") from None
            return existing
        _fsync_directory(target.parent)
        candidate.unlink()  # Remove our alias before the host opens its own WAL namespace.
        result = {**inspect_database(target, **scope, writable=True), "status": "installed"}
    _fsync_directory(target.parent)
    return result
