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

Mutations use consistent reads and conditional revisions, retrying a callback
against fresh data on conflict. File mutations lock across processes. Plain
load/save is explicit replacement, not a read-modify-write transaction. The
400 KB DynamoDB item limit still matters: row caps do NOT guarantee it fits;
oversized writes are rejected rather than silently pruning additional records.
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import random
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager

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
_file_locks = {}
_file_locks_guard = threading.Lock()


def _table():
    from aws.dynamodb_access import get_table
    return get_table(os.getenv(_TABLE_ENV) or _DEFAULT_TABLE)


def reset_cache() -> None:
    """Drop the memoized table handle (tests that switch backends mid-run)."""
    from aws.dynamodb_access import reset_tables
    reset_tables()


@contextmanager
def _file_transaction(path):
    path = os.path.abspath(path)
    with _file_locks_guard:
        lock = _file_locks.setdefault(path, threading.RLock())
    if not lock.acquire(timeout=8):
        raise TimeoutError("Storage is busy.")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a+b") as handle:
            # Windows byte-range locks may extend past EOF. Writing a marker
            # first races with another process already holding that byte's lock.
            deadline = time.monotonic() + 8
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Storage is busy.") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()


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

    # -- reading ----------------------------------------------------------
    def load(self) -> dict:
        if _backend() == "dynamodb":
            return self._load_ddb()
        return self._load_file()

    def _load_file(self, strict=False) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
                raise ValueError("Store must contain a JSON object.")
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

    def _load_ddb(self) -> dict:
        try:
            resp = _table().get_item(Key={"store": self.name}, ConsistentRead=True)
            item = resp.get("Item")
            if item and item.get("payload"):
                data = json.loads(item["payload"])
                if isinstance(data, dict):
                    return data
        except Exception as e:  # noqa: BLE001 - read-only lookup degrades gracefully
            # Same reasoning as above, one step more important: a DynamoDB
            # throttle or a dropped connection must not turn into a failed answer.
            print(f"  [store:{self.name}] DynamoDB read failed: {e}")
        return self._default()

    # -- writing ----------------------------------------------------------
    def save(self, data: dict) -> None:
        data["updated_at"] = _utcnow()
        if _backend() == "dynamodb":
            self._save_ddb(data)
        else:
            with _file_transaction(self.path):
                self._save_file(data)

    def _save_file(self, data: dict) -> None:
        # Write-then-rename: a crash mid-write leaves the previous good file
        # rather than a truncated one.
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=parent,
                prefix=os.path.basename(self.path) + ".", suffix=".tmp", delete=False,
            ) as fh:
                tmp = fh.name
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def _save_ddb(self, data: dict, **conditions) -> None:
        # Stored as a JSON string rather than a native map. DynamoDB rejects empty
        # strings in some contexts, coerces numbers to Decimal, and would force
        # every caller to sanitise its dict before saving; a blob round-trips
        # byte-for-byte, which is what "the file used to work like this" requires.
        payload = json.dumps(data, ensure_ascii=False)
        if len(payload.encode("utf-8")) > 390 * 1024:
            raise ValueError("Store exceeds the safe DynamoDB item-size budget.")
        _table().put_item(Item={
            "store": self.name,
            "payload": payload,
            "updated_at": data["updated_at"],
            "revision": uuid.uuid4().hex,
        }, **conditions)

    # -- convenience ------------------------------------------------------
    def mutate(self, fn: Callable[[dict], object], *, return_result=False):
        """
        Cross-process transaction. DynamoDB conflicts rerun fn on fresh data.

        `fn` may return False to abort the write, which is how "delete something
        that was not there" avoids an unnecessary round-trip while still being
        idempotent for the caller.
        """
        if _backend() != "dynamodb":
            with _file_transaction(self.path):
                data = self._load_file(strict=True)
                before = copy.deepcopy(data)
                result = fn(data)
                if result is not False and data != before:
                    data["updated_at"] = _utcnow()
                    self._save_file(data)
                return result if return_result else data

        deadline = time.monotonic() + 8
        for attempt in range(30):
            # Never use load() here: its graceful read fallback must not turn a
            # throttle or corrupt document into an empty store that overwrites data.
            item = _table().get_item(Key={"store": self.name}, ConsistentRead=True).get("Item")
            data = json.loads(item["payload"]) if item is not None else self._default()
            if not isinstance(data, dict):
                raise TypeError("Store must contain a JSON object.")
            before = copy.deepcopy(data)
            result = fn(data)
            if result is False or data == before:
                return result if return_result else data
            data["updated_at"] = _utcnow()
            if item is None:
                conditions = {"ConditionExpression": "attribute_not_exists(#pk)",
                              "ExpressionAttributeNames": {"#pk": "store"}}
            elif "revision" in item:
                conditions = {"ConditionExpression": "#rev = :rev",
                              "ExpressionAttributeNames": {"#rev": "revision"},
                              "ExpressionAttributeValues": {":rev": item["revision"]}}
            else:
                # Upgrade existing, unversioned records without a migration or
                # overwriting a concurrent legacy edit made after our read.
                conditions = {
                    "ConditionExpression": "attribute_not_exists(#rev) AND #payload = :payload",
                    "ExpressionAttributeNames": {"#rev": "revision", "#payload": "payload"},
                    "ExpressionAttributeValues": {":payload": item["payload"]},
                }
            try:
                self._save_ddb(data, **conditions)
                return result if return_result else data
            except Exception as error:
                code = getattr(error, "response", {}).get("Error", {}).get("Code")
                if code != "ConditionalCheckFailedException":
                    raise
                if attempt == 29 or time.monotonic() >= deadline:
                    raise TimeoutError("Store changed repeatedly; please retry.") from error
                time.sleep(random.uniform(0.001, min(0.15, 0.005 * (attempt + 1))))


def describe_backend() -> dict:
    """Small helper so an admin screen can show where state is actually going."""
    backend = _backend()
    return {
        "backend": backend,
        "table": os.getenv(_TABLE_ENV) or _DEFAULT_TABLE if backend == "dynamodb" else "",
        "shared_across_instances": backend == "dynamodb",
    }
