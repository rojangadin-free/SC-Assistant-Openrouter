"""
rag/jobs.py — progress state for uploads that are still being indexed.

Why this exists
---------------
`/upload` used to do everything inside the request: save the file, OCR it, chunk
it, embed it, upsert it to Pinecone, and only then reply. The browser's progress
bar tracked `xhr.upload.progress`, which measures *bytes leaving the browser* —
so on a LAN it hit 100% in about a second and then sat there, "finished", while
the real work had barely started. The bar was not inaccurate by a few percent;
it was measuring a different thing entirely.

That had a worse consequence than a lying bar. The Dockerfile runs gunicorn with
`--timeout 120`. A large scanned PDF can spend longer than that in OCR, so
gunicorn would kill the worker *mid-index*: the admin sees a failed upload for a
file whose chunks are already half-written to Pinecone. Moving the work off the
request removes the deadline, and this module is what lets the browser still see
what is happening.

Why the shared store rather than a dict
---------------------------------------
The obvious implementation is a module-level `_JOBS = {}`. It would work on a
laptop and fail in production, because gunicorn runs `--workers 2`: the POST that
starts the job can land on worker A while the follow-up status poll lands on
worker B, which has never heard of that job id and would honestly report "not
found". Reusing `JsonBlobStore` — the same seam `conflicts`, `gaps` and
`feedback` already use — means the state is visible to every worker, and setting
`STORE_BACKEND=dynamodb` makes it visible across instances too, with no change
here.

Honesty rules baked in
----------------------
* **Percentages come from real counters.** `pages_done/pages_total` while
  extracting, `batches_done/batches_total` while upserting. The phase weights
  below are a fixed, documented mapping, not a timer pretending to be progress.
* **Nothing reports 100% until it is indexed.** The old code set the bar to 100%
  in its `success` handler and hid it 500 ms later, so the only truthful frame
  was invisible.
* **A crashed job stops claiming to be running.** If the container restarts
  mid-index the thread dies with no chance to record failure, and a naive reader
  would show "Extracting page 12/40" forever. `_settle()` treats a job that has
  not been touched in `STALE_AFTER_SECONDS` as failed, so the UI can say so.
"""

from __future__ import annotations

import datetime
import os
import threading
import uuid
from typing import Callable, Dict, List, Optional

from rag.store import JsonBlobStore

JOBS_FILE = os.getenv("INDEX_JOBS_FILE", "index_jobs.json")

# This is a progress display, not an audit log. The feedback/gaps stores are the
# permanent record; a finished upload's page counts stop being interesting the
# moment the admin sees the file in the list.
MAX_JOBS = 25

# A job whose heartbeat is older than this is presumed dead (worker recycled,
# container redeployed, OOM). Generous enough to cover a slow OCR page on a
# small EC2 box — the writes below happen per page, not per file.
STALE_AFTER_SECONDS = 300

# Phase -> (percent at phase start, percent at phase end). Extraction owns the
# largest span because on a scanned PDF it genuinely is most of the wall time;
# OCR dominates everything else. `indexing` gets a real span rather than a token
# one because embedding 100-chunk batches is slow enough to watch.
_PHASE_SPAN = {
    "queued":     (0, 0),
    "extracting": (5, 70),
    "chunking":   (70, 75),
    "indexing":   (75, 98),
    "storing":    (98, 99),
    "done":       (100, 100),
    "error":      (0, 0),
}

_PHASE_LABEL = {
    "queued":     "Waiting",
    "extracting": "Reading document",
    "chunking":   "Splitting into passages",
    "indexing":   "Building search index",
    "storing":    "Saving file",
    "done":       "Indexed",
    "error":      "Failed",
}

_lock = threading.Lock()
_store_obj: Optional[JsonBlobStore] = None
_store_obj_path: Optional[str] = None


def _empty_store() -> dict:
    return {"jobs": {}}


def _store() -> JsonBlobStore:
    """Memoised, but re-created if the configured path changed (tests)."""
    global _store_obj, _store_obj_path
    path = os.getenv("INDEX_JOBS_FILE", JOBS_FILE)
    with _lock:
        if _store_obj is None or _store_obj_path != path:
            _store_obj = JsonBlobStore("index_jobs", path, _empty_store)
            _store_obj_path = path
        return _store_obj


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _age_seconds(ts: str) -> float:
    try:
        then = datetime.datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()
    except Exception:
        # An unparseable timestamp should not make a job immortal.
        return STALE_AFTER_SECONDS + 1


# ============================================================
# PERCENT
# ============================================================

def _file_percent(f: dict) -> int:
    """
    One file's completion, from whatever counter its current phase exposes.

    Falls back to the start of the phase's span when a counter is missing —
    which is the honest answer for "we are in this phase but cannot yet say how
    far", and never overstates.
    """
    phase = f.get("phase") or "queued"
    lo, hi = _PHASE_SPAN.get(phase, (0, 0))
    if phase == "done":
        return 100
    if phase == "error":
        return int(f.get("percent") or 0)

    done = f.get("units_done")
    total = f.get("units_total")
    if isinstance(done, int) and isinstance(total, int) and total > 0:
        ratio = max(0.0, min(1.0, done / total))
        return int(lo + (hi - lo) * ratio)
    return int(lo)


def _job_percent(job: dict) -> int:
    """
    Whole-job completion: the mean across files, so a two-file upload does not
    jump to 100% when the first finishes.
    """
    files = job.get("files") or []
    if not files:
        return 0
    return int(sum(_file_percent(f) for f in files) / len(files))


def _settle(job: dict) -> dict:
    """
    Mark a stalled job failed instead of letting it claim to be running forever.

    Applied on read, not by a sweeper: the only moment anybody cares whether a
    job is dead is when someone asks about it, and a background reaper would be
    one more thing to run and get wrong.
    """
    if job.get("status") != "running":
        return job
    if _age_seconds(job.get("updated_at") or job.get("created_at") or "") <= STALE_AFTER_SECONDS:
        return job

    job["status"] = "error"
    job["error"] = (
        "Indexing stopped unexpectedly — the server may have restarted. "
        "The file was not fully indexed; please upload it again."
    )
    for f in job.get("files") or []:
        if f.get("status") in ("queued", "running"):
            f["status"] = "error"
            f["phase"] = "error"
            f["error"] = "Interrupted"
    return job


# ============================================================
# WRITING
# ============================================================

def create_job(filenames: List[str]) -> str:
    """Register a job in `queued` before any worker thread starts."""
    job_id = uuid.uuid4().hex[:12]
    now = _utcnow()
    job = {
        "job_id": job_id,
        "created_at": now,
        "updated_at": now,
        "status": "running",
        "error": "",
        "files": [
            {
                "filename": name,
                "status": "queued",
                "phase": "queued",
                "units_done": None,
                "units_total": None,
                "chunks": 0,
                "error": "",
            }
            for name in filenames
        ],
    }

    def _add(data: dict) -> None:
        jobs = data.setdefault("jobs", {})
        jobs[job_id] = job
        # Prune oldest first so the store cannot grow without bound.
        if len(jobs) > MAX_JOBS:
            for stale_id, _ in sorted(
                jobs.items(), key=lambda kv: kv[1].get("created_at") or ""
            )[: len(jobs) - MAX_JOBS]:
                jobs.pop(stale_id, None)

    _store().mutate(_add)
    return job_id


def _mutate_file(job_id: str, filename: str, apply: Callable[[dict], None]) -> None:
    def _fn(data: dict):
        job = (data.get("jobs") or {}).get(job_id)
        if not job:
            return False
        for f in job.get("files") or []:
            if f.get("filename") == filename:
                apply(f)
                break
        else:
            return False
        job["updated_at"] = _utcnow()
        return None

    _store().mutate(_fn)


def set_phase(job_id: str, filename: str, phase: str,
              *, units_done: Optional[int] = None,
              units_total: Optional[int] = None) -> None:
    """
    Move a file into a phase, optionally with the counter that drives its bar.

    Called on every extracted page and every upserted batch, so it must stay
    cheap: one load-mutate-save on a small JSON document. That is fine for a file
    mode store and acceptable for DynamoDB at page cadence, which is why progress
    is reported per page rather than per paragraph.
    """
    def _apply(f: dict) -> None:
        f["status"] = "running"
        f["phase"] = phase
        f["units_done"] = units_done
        f["units_total"] = units_total
        f["percent"] = _file_percent(f)

    _mutate_file(job_id, filename, _apply)


def finish_file(job_id: str, filename: str, chunks: int) -> None:
    def _apply(f: dict) -> None:
        f["status"] = "done"
        f["phase"] = "done"
        f["units_done"] = None
        f["units_total"] = None
        f["chunks"] = int(chunks)
        f["percent"] = 100

    _mutate_file(job_id, filename, _apply)


def fail_file(job_id: str, filename: str, message: str) -> None:
    def _apply(f: dict) -> None:
        f["percent"] = _file_percent(f)   # freeze the bar where it actually got to
        f["status"] = "error"
        f["phase"] = "error"
        f["error"] = str(message)[:500]

    _mutate_file(job_id, filename, _apply)


def finish_job(job_id: str) -> None:
    """
    Close the job, deriving its status from its files.

    A partial failure stays visible as `error` even though other files
    succeeded — "3 of 4 indexed" is the truth, and reporting success would hide
    the one file an admin needs to re-upload.
    """
    def _fn(data: dict):
        job = (data.get("jobs") or {}).get(job_id)
        if not job:
            return False
        files = job.get("files") or []
        failed = [f for f in files if f.get("status") == "error"]
        job["status"] = "error" if failed else "done"
        if failed:
            job["error"] = "; ".join(
                f"{f.get('filename')}: {f.get('error') or 'failed'}" for f in failed
            )[:1000]
        job["updated_at"] = _utcnow()
        return None

    _store().mutate(_fn)


def fail_job(job_id: str, message: str) -> None:
    """For a failure outside any single file (bad job, thread died on setup)."""
    def _fn(data: dict):
        job = (data.get("jobs") or {}).get(job_id)
        if not job:
            return False
        job["status"] = "error"
        job["error"] = str(message)[:1000]
        job["updated_at"] = _utcnow()
        return None

    _store().mutate(_fn)


# ============================================================
# READING
# ============================================================

def get_job(job_id: str) -> Optional[dict]:
    """
    A job shaped for the UI: labels and percentages resolved server-side.

    The client should not own the phase->label or phase->percent mapping; if it
    did, the bar's meaning would live in two places and drift the first time a
    phase is added.
    """
    job = (_store().load().get("jobs") or {}).get(job_id)
    if not job:
        return None

    job = _settle(dict(job))
    files: List[Dict] = []
    for f in job.get("files") or []:
        f = dict(f)
        pct = _file_percent(f)
        detail = ""
        if f.get("phase") == "extracting" and isinstance(f.get("units_total"), int):
            detail = f"page {f.get('units_done') or 0} of {f['units_total']}"
        elif f.get("phase") == "indexing" and isinstance(f.get("units_total"), int):
            detail = f"batch {f.get('units_done') or 0} of {f['units_total']}"
        elif f.get("phase") == "done":
            detail = f"{f.get('chunks') or 0} passages indexed"

        files.append({
            "filename": f.get("filename"),
            "status": f.get("status"),
            "phase": f.get("phase"),
            "label": _PHASE_LABEL.get(f.get("phase") or "queued", "Working"),
            "detail": detail,
            "percent": pct,
            "chunks": f.get("chunks") or 0,
            "error": f.get("error") or "",
        })

    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "error": job.get("error") or "",
        "percent": _job_percent(job),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "files": files,
        "done": job.get("status") in ("done", "error"),
    }
