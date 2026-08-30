"""
sc_assistant/admin_conflicts.py — admin API for the Data Conflicts screen.

Flow
----
POST /admin/conflicts/api/scan     -> read the indexed documents, extract role
                                      facts, return every contradiction
POST /admin/conflicts/api/resolve  -> pin the correct value for one conflict
POST /admin/conflicts/api/unresolve-> drop a decision (re-open the conflict)
GET  /admin/conflicts/api/resolutions -> the current decision list

The scan reads the documents from S3 (the same files that were indexed), so what
the admin reviews is what the assistant actually retrieves. Results are cached in
memory because a scan opens every PDF; the UI has an explicit refresh.
"""

from __future__ import annotations

import os
import tempfile
import threading
import datetime

from flask import Blueprint, jsonify, request, session

from .utils import is_admin
from rag.conflicts import (
    extract_facts,
    find_conflicts,
    explain_for_admin,
    list_resolutions,
    set_resolution,
    delete_resolution,
)

bp_conflicts = Blueprint("conflicts", __name__, url_prefix="/admin/conflicts")

SCANNABLE_EXT = (".pdf", ".docx", ".doc", ".txt", ".md")

# Cached last scan: {"conflicts": [...], "scanned_at": iso, "documents": [...]}
_cache: dict = {}
_cache_lock = threading.Lock()


def _require_admin() -> bool:
    return bool(session.get("user")) and is_admin()


# ---------------------------------------------------------------------------
# Document loading
# ---------------------------------------------------------------------------

def _pages_from_pdf(path: str, name: str):
    import pymupdf

    doc = pymupdf.open(path)
    try:
        for i in range(doc.page_count):
            yield name, i + 1, doc.load_page(i).get_text("text")
    finally:
        doc.close()


def _pages_from_docx(path: str, name: str):
    from docx import Document as DocxDocument

    d = DocxDocument(path)
    yield name, None, "\n".join(p.text for p in d.paragraphs)


def _pages_from_txt(path: str, name: str):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        yield name, None, fh.read()


def _pages(path: str, name: str):
    ext = os.path.splitext(name)[1].lower()
    if ext == ".pdf":
        yield from _pages_from_pdf(path, name)
    elif ext in (".docx", ".doc"):
        yield from _pages_from_docx(path, name)
    elif ext in (".txt", ".md"):
        yield from _pages_from_txt(path, name)


def _scan_sources() -> tuple[list, list, list]:
    """
    Returns (facts, conflicts, document_names).

    Prefers the S3 corpus (what is indexed). Falls back to the local `data/`
    folder so the screen still works in local development without AWS.
    """
    facts = []
    names: list[str] = []

    downloaded: list[tuple[str, str]] = []   # (local_path, display_name)
    tmpdir = None

    try:
        import boto3
        from config import S3_BUCKET_NAME

        s3 = boto3.client("s3")
        tmpdir = tempfile.mkdtemp(prefix="sc_conflicts_")
        token = None
        while True:
            kw = {"Bucket": S3_BUCKET_NAME}
            if token:
                kw["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                if not key.lower().endswith(SCANNABLE_EXT):
                    continue
                base = os.path.basename(key)
                dest = os.path.join(tmpdir, base)
                s3.download_file(S3_BUCKET_NAME, key, dest)
                downloaded.append((dest, base))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
    except Exception as e:
        print(f"[conflicts] S3 scan unavailable, falling back to data/: {e}")

    if not downloaded and os.path.isdir("data"):
        for f in sorted(os.listdir("data")):
            if f.lower().endswith(SCANNABLE_EXT):
                downloaded.append((os.path.join("data", f), f))

    for path, name in downloaded:
        names.append(name)
        try:
            for source, page, text in _pages(path, name):
                facts.extend(extract_facts(text, source=source, page=page))
        except Exception as e:
            print(f"[conflicts] could not read {name}: {e}")

    if tmpdir:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    return facts, find_conflicts(facts), names


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp_conflicts.route("/api/scan", methods=["GET", "POST"])
def api_scan():
    """
    Scan for contradictions. `?refresh=1` (or POST) forces a re-read of the
    documents; otherwise the cached result is returned so switching sections in
    the dashboard is instant.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    force = request.method == "POST" or request.args.get("refresh") == "1"

    with _cache_lock:
        if not force and _cache.get("conflicts") is not None:
            payload = dict(_cache)
            # Decisions can change without a rescan — always re-merge them.
            payload["conflicts"] = explain_for_admin(_cache["_objects"])
            payload["success"] = True
            payload.pop("_objects", None)
            return jsonify(payload)

    try:
        facts, conflicts, names = _scan_sources()
    except Exception as e:
        return jsonify({"success": False, "message": f"Scan failed: {e}"}), 500

    payload = {
        "success": True,
        "documents": names,
        "fact_count": len(facts),
        "conflicts": explain_for_admin(conflicts),
        "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    with _cache_lock:
        _cache.clear()
        _cache.update({
            "documents": names,
            "fact_count": len(facts),
            "conflicts": payload["conflicts"],
            "scanned_at": payload["scanned_at"],
            "_objects": conflicts,
        })

    return jsonify(payload)


@bp_conflicts.route("/api/resolutions")
def api_resolutions():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    return jsonify({"success": True, "resolutions": list_resolutions()})


@bp_conflicts.route("/api/resolve", methods=["POST"])
def api_resolve():
    """
    Body: {key, correct, rejected:[...], role, subject, note}

    `correct` may be one of the detected candidates or a value the admin types
    in — the document can be wrong in BOTH places, and forcing a choice between
    two wrong names would make the tool useless.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    correct = (data.get("correct") or "").strip()

    if not key or "|" not in key:
        return jsonify({"success": False, "message": "A conflict key is required."}), 400
    if not correct:
        return jsonify({"success": False, "message": "Select or type the correct value."}), 400

    entry = set_resolution(
        key,
        correct,
        role=data.get("role", ""),
        subject=data.get("subject", ""),
        note=data.get("note", ""),
        resolved_by=session.get("user", ""),
        rejected=data.get("rejected") or [],
    )
    return jsonify({"success": True, "resolution": entry,
                    "message": f"Saved. The assistant will now answer \"{correct}\"."})


@bp_conflicts.route("/api/unresolve", methods=["POST"])
def api_unresolve():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"success": False, "message": "A conflict key is required."}), 400

    removed = delete_resolution(key)
    return jsonify({
        "success": True,
        "removed": removed,
        "message": "Decision removed." if removed else "No decision was saved for that item.",
    })
