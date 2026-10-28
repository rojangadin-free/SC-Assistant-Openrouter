"""
rag/store.py — one storage seam for every admin-decision file in `rag/`.

The problem this exists to fix
------------------------------
`conflicts.py`, `gaps.py`, `feedback.py` and `escalation.py` each keep their state
in a JSON file next to the app. That is genuinely the right choice for a single
process — zero setup, readable with `cat`, and the whole dataset is a handful of
rows. It stops being right the moment the app runs as more than one container.

`Dockerfile` builds exactly that kind of deployment. Two instances behind a load
balancer each get their own writable layer, so:

    admin (hits instance A) pins "Dean of CoEd = Jacqueline Montalis"
    student (hits instance B) asks who the dean is
    -> instance B never saw the decision, and answers with the wrong name

Nothing errors. No log line appears. The feature simply does not work for roughly
half the traffic, and the failure is invisible to whoever tested it (they were
almost certainly pinned to one instance by their own session). That is the worst
class of bug in this codebase, so it is worth removing even though the local
experience is currently fine.

Why a shared backend instead of a shared volume
-----------------------------------------------
An EFS/NFS mount would also fix visibility, but `os.replace` + a `threading.Lock`
only serialises writers *inside one process*. Two containers doing
read-modify-write on the same file will still lose one of the two updates, and the
one that vanishes is an admin's decision. DynamoDB gives per-item atomicity, and
these stores are already keyed by an obvious partition key.

Design
------
`JsonBlobStore` is deliberately not an ORM. Each caller keeps its existing
`{"resolutions": {...}}` / `{"votes": {...}}` shaped dict and simply asks this
module to load and save it. That keeps the change surface to two functions per
module, keeps every test that pokes at the dict shape working, and means the
migration is reversible by flipping one environment variable.

    STORE_BACKEND=file      (default) — behaves exactly as before
    STORE_BACKEND=dynamodb            — one item per store, shared by all instances

The default is `file` on purpose. Local development, `run_tests.py` and the
project defence demo all run one process with no AWS credentials, and a storage
layer that requires a cloud account to start is worse than one that does not
scale.

Why one item per store, not one item per row
--------------------------------------------
These datasets are small (bounded at a few hundred rows by MAX_VOTES and friends)
and are almost always read whole: `authority_block()` needs every resolution to
match against a question, the admin screens list everything. One item is one
network round-trip instead of a scan, and it preserves the "load the dict, mutate,
save the dict" pattern the callers already use, so aggregate rebuilds
(`_rebuild_topic()`) keep working untouched.

The cost is the 400 KB item limit. Writes reject oversized payloads; the feedback
sample is additionally byte-bounded. Mutation callbacks replay after a version
conflict, so simultaneous writers recompute against current data rather than
overwrite another key. File mode provides process locks and atomic replacement.
The low-level save() operation replaces a snapshot and is for fixtures/imports;
production read-modify-write operations use mutate() or atomic_mutation().
"""

from __future__ import annotations

import datetime
import functools
import json
import os
import random
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Optional
from rag.runtime import env_int

# The switch. Read at call time rather than import time so a test can flip it.
_BACKEND_ENV = "STORE_BACKEND"
_TABLE_ENV = "STORE_TABLE_NAME"
_DEFAULT_TABLE = "SCAssistantStores"


def _backend() -> str:
    return (os.getenv(_BACKEND_ENV) or "file").strip().lower()


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DynamoDB is imported lazily and only when actually selected.
#
# `rag.conflicts` is imported by the prompt builder on every request, and this
# module is imported by it. A module-level `import boto3` would make the entire
# RAG pipeline — and every test that touches it — depend on boto3 being installed
# and credentials being present, to support a backend most runs do not use.
# ---------------------------------------------------------------------------
_ddb_lock = threading.Lock()
_ddb_table = None


def _table():
    global _ddb_table
    with _ddb_lock:
        if _ddb_table is None:
            import boto3  # noqa: WPS433 — deliberately deferred, see above
            from botocore.config import Config
            from config import AWS_REGION

            name = os.getenv(_TABLE_ENV) or _DEFAULT_TABLE
            _ddb_table = boto3.resource("dynamodb", region_name=AWS_REGION, config=Config(
                connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}
            )).Table(name)
        return _ddb_table


def reset_cache() -> None:
    """Drop the memoized table handle (tests that switch backends mid-run)."""
    global _ddb_table
    with _ddb_lock:
        _ddb_table = None


class JsonBlobStore:
    """
    One named JSON document, kept either in a local file or in one DynamoDB item.

    `name`     — the partition key when stored in DynamoDB ("conflicts", "gaps"…).
    `path`     — the file to use in `file` mode. Callers pass the same env-var
                 driven path they used before, so nothing about local runs changes.
    `default`  — a factory, not a literal: returning a shared mutable dict from
                 `load()` would let one caller's mutation leak into the next
                 caller's "empty" store, which is the kind of bug that only shows
                 up under load.
    """

    def __init__(self, name: str, path: str, default: Callable[[], dict]):
        self.name = name
        self.path = path
        self._default = default
        # Still needed in file mode, and harmless in DynamoDB mode: it keeps a
        # single process's own read-modify-write cycles serialised.
        self.lock = threading.Lock()
        self._local = threading.local()

    # -- reading ----------------------------------------------------------
    def load(self) -> dict:
        active = getattr(self._local, "data", None)
        if active is not None:
            return active
        if _backend() == "dynamodb":
            return self._load_ddb()
        return self._load_file()

    def _load_file(self, strict=False) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
                if strict:
                    raise ValueError("Stored JSON must be an object")
        except FileNotFoundError:
            pass
        except Exception as e:
            if strict:
                raise
            # A corrupt or half-written file must not take the app down: the
            # assistant answering questions matters more than an admin's
            # bookkeeping, and returning the default degrades to "no decisions
            # recorded" rather than a 500 on every request.
            print(f"  [store:{self.name}] could not read {self.path}: {e}")
        return self._default()

    def _read_ddb(self):
        resp = _table().get_item(Key={"store": self.name}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return self._default(), None
        data = json.loads(item["payload"])
        if not isinstance(data, dict):
            raise ValueError("Stored JSON must be an object")
        return data, item.get("version")

    def _load_ddb(self) -> dict:
        try:
            return self._read_ddb()[0]
        except Exception as e:
            # Same reasoning as above, one step more important: a DynamoDB
            # throttle or a dropped connection must not turn into a failed answer.
            print(f"  [store:{self.name}] DynamoDB read failed: {e}")
        return self._default()

    # -- writing ----------------------------------------------------------
    def save(self, data: dict) -> None:
        active = getattr(self._local, "data", None)
        if active is not None:
            if data is not active:
                active.clear()
                active.update(data)
            self._local.dirty = True
            return
        with self.lock:
            data["updated_at"] = _utcnow()
            if _backend() == "dynamodb":
                _, version = self._read_ddb()
                self._save_ddb(data, version)
            else:
                with _file_lock(self.path):
                    self._save_file(data)

    def _save_file(self, data: dict) -> None:
        # Write-then-rename: a crash mid-write leaves the previous good file
        # rather than a truncated one.
        tmp = f"{self.path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _save_ddb(self, data: dict, version=None) -> None:
        # Stored as a JSON string rather than a native map. DynamoDB rejects empty
        # strings in some contexts, coerces numbers to Decimal, and would force
        # every caller to sanitise its dict before saving; a blob round-trips
        # byte-for-byte, which is what "the file used to work like this" requires.
        payload = json.dumps(data, ensure_ascii=False)
        if len(payload.encode("utf-8")) + len(self.name.encode("utf-8")) > 380 * 1024:
            raise ValueError("Shared store exceeds its safe DynamoDB item-size budget")
        item = {
            "store": self.name,
            "payload": payload,
            "updated_at": data["updated_at"],
            "version": int(version or 0) + 1,
        }
        if isinstance(data.get("expires_at"), int):
            item["expires_at"] = data["expires_at"]
        arguments = {
            "Item": item,
            "ConditionExpression": "attribute_not_exists(#version)" if version is None else "#version = :expected",
            "ExpressionAttributeNames": {"#version": "version"},
        }
        if version is not None:
            arguments["ExpressionAttributeValues"] = {":expected": version}
        _table().put_item(**arguments)

    # -- convenience ------------------------------------------------------
    def mutate(self, fn: Callable[[dict], Optional[bool]]) -> dict:
        """
        Strict read -> fn(store) -> conditional save, replaying on conflicts.
        Callbacks must not perform network calls or other irreversible effects.

        `fn` may return False to abort the write, which is how "delete something
        that was not there" avoids an unnecessary round-trip while still being
        idempotent for the caller.
        """
        active = getattr(self._local, "data", None)
        if active is not None:
            if fn(active) is not False:
                self._local.dirty = True
            return active
        with self.lock:
            if _backend() != "dynamodb":
                with _file_lock(self.path):
                    data = self._load_file(strict=True)
                    if self._apply(fn, data) is not False:
                        data["updated_at"] = _utcnow()
                        self._save_file(data)
                    return data
            for attempt in range(env_int("STORE_CAS_RETRIES", 20)):
                data, version = self._read_ddb()
                if self._apply(fn, data) is False:
                    return data
                data["updated_at"] = _utcnow()
                try:
                    self._save_ddb(data, version)
                    return data
                except Exception as exc:
                    if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
                    time.sleep(random.uniform(0, 0.005 * min(attempt + 1, 10)))
            raise RuntimeError("Shared state is busy; please retry the update")

    def _apply(self, fn, data):
        self._local.data, self._local.dirty = data, False
        try:
            return fn(data)
        finally:
            self._local.data = None


@contextmanager
def _file_lock(path):
    """Lock spans independent objects and processes, not just threads."""
    with open(path + ".lock", "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 10
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Local shared store is busy")
                time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_mutation(store_factory, swallow=False):
    """Replay pure domain mutations on refreshed state after CAS conflicts.

    Existing load/save helpers see transaction-local data, so aggregates and
    return values are recomputed, not merged from an obsolete snapshot.
    """
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            store = store_factory()
            result = [None]

            def execute(data):
                result[0] = function(*args, **kwargs)
                return None if store._local.dirty else False

            try:
                store.mutate(execute)
                return result[0]
            except Exception as exc:
                if not swallow:
                    raise
                print(f"[store:{store.name}] update failed: {exc}")
                return None
        return wrapped
    return decorate


def describe_backend() -> dict:
    """Small helper so an admin screen can show where state is actually going."""
    backend = _backend()
    return {
        "backend": backend,
        "table": os.getenv(_TABLE_ENV) or _DEFAULT_TABLE if backend == "dynamodb" else "",
        "shared_across_instances": backend == "dynamodb",
    }
