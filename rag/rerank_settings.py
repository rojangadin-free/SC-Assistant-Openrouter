"""
rag/rerank_settings.py — which reranker is live, changeable at runtime.

Why this file exists
--------------------
There are now two rerankers in this project and they are good at different
things:

    local   `rag/reranker.py`      cross-encoder on this box's CPU.
                                   No network, no quota, no per-request cost,
                                   but ~3.5 s of contended CPU per question on
                                   the 2-vCPU deployment target and ~180 MB of
                                   resident weights per worker.

    api     `rag/api_reranker.py`  OpenRouter `/api/v1/rerank`, model
                                   `nvidia/llama-nemotron-rerank-vl-1b-v2:free`.
                                   A 1B reranker for the CPU cost of an HTTPS
                                   round-trip, but it needs egress, a key, and
                                   it is subject to someone else's rate limits.

Neither is correct in every situation, which is the whole point: on a laptop with
no HuggingFace cache the API one works and the local one does not; on an exam-day
traffic spike against a free-tier quota the local one works and the API one does
not. So the choice is a SETTING, not a constant.

Why a store rather than only an env var
---------------------------------------
An env var is a redeploy. The two reasons you would want to switch — "the free
quota just ran out" and "this box is pegged at 100% CPU" — both arrive while the
app is running and students are waiting, and neither is worth a container restart
that drops every open SSE stream.

So the value lives in `rag.store`, the same seam every other admin decision in
this project uses (see docs/SHARED_STORAGE.md). That buys the thing an env var
cannot: with STORE_BACKEND=dynamodb the switch applies to EVERY instance, not
just the one the admin's session happened to land on. A per-process toggle would
be worse than no toggle, because the admin would see it take effect (their own
worker) while half the students kept the old behaviour.

`RERANKER_BACKEND` is still read, and it is what the store falls back to, so a
deployment can still ship a default without ever opening the dashboard.

Caching
-------
`current()` is called on the hot path — once per question — and in DynamoDB mode
each uncached read is a network round-trip. The value changes approximately never
(an admin clicking a switch), so it is cached for `_TTL_SECONDS` and invalidated
immediately on write. The TTL is what makes a change made on instance A visible
on instance B without either of them being restarted.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

from rag.store import JsonBlobStore

# The two supported values. Deliberately short strings: they end up in a log
# line, in a JSON payload and in a radio input's value, and "local"/"api" reads
# the same in all three.
LOCAL = "local"
API = "api"
BACKENDS = (LOCAL, API)

SETTINGS_FILE = os.getenv("RERANK_SETTINGS_FILE", "rerank_settings.json")

# The default when nothing has ever been saved. `local` matches the behaviour
# this project had before the API reranker existed, so an existing deployment
# that upgrades sees no change until someone deliberately asks for one.
DEFAULT_BACKEND = (os.getenv("RERANKER_BACKEND", LOCAL) or LOCAL).strip().lower()
if DEFAULT_BACKEND not in BACKENDS:
    DEFAULT_BACKEND = LOCAL

# How long a read may be served from memory. Short enough that a switch on one
# instance reaches the others within seconds; long enough that a burst of
# questions does not become a burst of DynamoDB reads.
_TTL_SECONDS = float(os.getenv("RERANK_SETTINGS_TTL", "15"))

_lock = threading.Lock()
_store_obj: Optional[JsonBlobStore] = None
_store_obj_path: Optional[str] = None

_cached_value: Optional[str] = None
_cached_at: float = 0.0


def _empty_store() -> dict:
    return {"backend": DEFAULT_BACKEND, "updated_at": "", "updated_by": ""}


def _store() -> JsonBlobStore:
    """
    Built lazily, and rebuilt when the path changes.

    Lazily because tests point RERANK_SETTINGS_FILE at a temp file AFTER
    importing this module; a store constructed at import time would keep writing
    to the developer's real settings file during a test run.
    """
    global _store_obj, _store_obj_path
    path = os.getenv("RERANK_SETTINGS_FILE", SETTINGS_FILE)
    with _lock:
        if _store_obj is None or _store_obj_path != path:
            _store_obj = JsonBlobStore("rerank_settings", path, _empty_store)
            _store_obj_path = path
        return _store_obj


def _normalize(value) -> str:
    """
    Coerce anything to one of BACKENDS.

    Unknown values resolve to the DEFAULT rather than raising: this is read on
    the path that answers a student's question, and a hand-edited store or a
    half-written DynamoDB item must cost ranking quality at worst, never an
    answer.
    """
    v = (str(value or "")).strip().lower()
    return v if v in BACKENDS else DEFAULT_BACKEND


def current(refresh: bool = False) -> str:
    """
    The live backend: "local" or "api".

    `refresh=True` skips the cache — used by the admin screen, which must show
    what is actually stored rather than what this worker remembers.
    """
    global _cached_value, _cached_at

    now = time.time()
    if not refresh and _cached_value is not None and (now - _cached_at) < _TTL_SECONDS:
        return _cached_value

    try:
        data = _store().load()
        value = _normalize(data.get("backend"))
    except Exception as e:
        # Same rule as everywhere else in rag/: a storage problem degrades to the
        # default, it does not fail the request.
        print(f"  [rerank_settings] read failed, using {DEFAULT_BACKEND}: {e}")
        value = DEFAULT_BACKEND

    _cached_value = value
    _cached_at = now
    return value


def set_backend(backend: str, changed_by: str = "") -> str:
    """
    Persist the choice and return the value actually stored.

    Returns the normalized value rather than None so the caller can echo back
    what was saved — an admin who typed "API " must see "api" confirmed, not be
    left guessing whether the trailing space mattered.
    """
    global _cached_value, _cached_at

    value = (backend or "").strip().lower()
    if value not in BACKENDS:
        raise ValueError(f"Unknown reranker backend: {backend!r}. Use one of {BACKENDS}.")

    def _apply(store: dict):
        store["backend"] = value
        store["updated_by"] = changed_by or ""

    _store().mutate(_apply)

    # Invalidate immediately, so the admin's very next page load reflects the
    # click instead of waiting out the TTL on their own worker.
    _cached_value = value
    _cached_at = time.time()
    return value


def describe() -> dict:
    """
    Everything the admin screen needs, in one call.

    Includes the readiness of BOTH backends, not just the live one, because the
    question an admin actually has is "can I switch?" — and the honest answer
    depends on whether a key is configured and whether weights are on disk.
    """
    backend = current(refresh=True)

    try:
        data = _store().load()
    except Exception:
        data = _empty_store()

    info = {
        "backend": backend,
        "updated_at": data.get("updated_at", ""),
        "updated_by": data.get("updated_by", ""),
        "default": DEFAULT_BACKEND,
        "options": list(BACKENDS),
    }

    # Imported here, not at module scope. `rag.api_reranker` is cheap, but
    # `rag.reranker` pulls in the torch-adjacent module-level configuration and
    # this module is imported by the admin blueprint at boot.
    try:
        from rag import reranker as _local

        info["local"] = {
            "model": _local.RERANKER_MODEL_NAME,
            "enabled": _local.RERANKER_ENABLED,
            "ready": _local.available(),
            "reason": _local.unavailable_reason(),
        }
    except Exception as e:
        info["local"] = {"model": "", "enabled": False, "ready": False, "reason": str(e)}

    try:
        from rag import api_reranker as _api

        info["api"] = {
            "model": _api.API_RERANKER_MODEL,
            "configured": _api.configured(),
            "ready": _api.configured(),
            "reason": _api.unavailable_reason(),
        }
    except Exception as e:
        info["api"] = {"model": "", "configured": False, "ready": False, "reason": str(e)}

    return info


def reset_cache() -> None:
    """Drop the memoized value (tests, and anything that edits the store directly)."""
    global _cached_value, _cached_at
    _cached_value = None
    _cached_at = 0.0
