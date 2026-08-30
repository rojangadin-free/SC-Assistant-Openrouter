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

The cost is the 400 KB item limit and last-writer-wins between two simultaneous
admins. Both are acceptable here: 400 KB is far beyond the row caps, and two
admins resolving the *same* key in the same second is a coin flip either way —
what matters is that neither of them silently loses the *other* key's decision,
which per-item storage guarantees and a shared file does not.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Callable, Optional

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
            from config import AWS_REGION

            name = os.getenv(_TABLE_ENV) or _DEFAULT_TABLE
            _ddb_table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)
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

    # -- reading ----------------------------------------------------------
    def load(self) -> dict:
        if _backend() == "dynamodb":
            return self._load_ddb()
        return self._load_file()

    def _load_file(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except FileNotFoundError:
            pass
        except Exception as e:
            # A corrupt or half-written file must not take the app down: the
            # assistant answering questions matters more than an admin's
            # bookkeeping, and returning the default degrades to "no decisions
            # recorded" rather than a 500 on every request.
            print(f"  [store:{self.name}] could not read {self.path}: {e}")
        return self._default()

    def _load_ddb(self) -> dict:
        try:
            resp = _table().get_item(Key={"store": self.name})
            item = resp.get("Item")
            if item and item.get("payload"):
                data = json.loads(item["payload"])
                if isinstance(data, dict):
                    return data
        except Exception as e:
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
            self._save_file(data)

    def _save_file(self, data: dict) -> None:
        # Write-then-rename: a crash mid-write leaves the previous good file
        # rather than a truncated one.
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _save_ddb(self, data: dict) -> None:
        # Stored as a JSON string rather than a native map. DynamoDB rejects empty
        # strings in some contexts, coerces numbers to Decimal, and would force
        # every caller to sanitise its dict before saving; a blob round-trips
        # byte-for-byte, which is what "the file used to work like this" requires.
        _table().put_item(Item={
            "store": self.name,
            "payload": json.dumps(data, ensure_ascii=False),
            "updated_at": data["updated_at"],
        })

    # -- convenience ------------------------------------------------------
    def mutate(self, fn: Callable[[dict], Optional[bool]]) -> dict:
        """
        load -> fn(store) -> save, under this process's lock.

        `fn` may return False to abort the write, which is how "delete something
        that was not there" avoids an unnecessary round-trip while still being
        idempotent for the caller.
        """
        with self.lock:
            data = self.load()
            if fn(data) is False:
                return data
            self.save(data)
            return data


def describe_backend() -> dict:
    """Small helper so an admin screen can show where state is actually going."""
    backend = _backend()
    return {
        "backend": backend,
        "table": os.getenv(_TABLE_ENV) or _DEFAULT_TABLE if backend == "dynamodb" else "",
        "shared_across_instances": backend == "dynamodb",
    }
