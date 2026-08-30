"""
sc_assistant/admin_freshness.py — admin API for document dates and the pre-index
upload scan.

Endpoints
---------
GET  /admin/freshness/api/list                every document, newest date first
POST /admin/freshness/api/set-date            record or correct one date
POST /admin/freshness/api/delete              forget a document's date
POST /admin/freshness/api/scan-upload         dry-run a file BEFORE indexing it
GET  /admin/freshness/api/preview?sources=    the prompt block, as the model sees it

The one that matters is `scan-upload`. It takes the file, reads it, extracts its
role assertions, compares them against what is already indexed, and returns the
contradictions — **without writing a single vector**. The admin then decides,
with the document still in front of them, which value is current.

Why the scan is a separate call from the upload
-----------------------------------------------
It could have been folded into `/admin/upload` as a "warn and continue" step. It
is not, because a warning attached to a completed action is a warning nobody acts
on: the file is already indexed, students are already getting the contradictory
answer, and the admin's remaining options all involve cleanup. Keeping the scan
as its own call means the decision happens while it is still cheap — nothing has
been written, so "cancel" is free.

It also means the existing upload route keeps working untouched for the 95% of
files that contradict nothing.
"""

from __future__ import annotations

import os
import tempfile

from flask import Blueprint, jsonify, request, session

from .utils import is_admin
from rag.conflicts import extract_facts
from rag.freshness import (
    delete_doc,
    freshness_block,
    list_docs,
    preview_upload,
    set_doc_date,
)

bp_freshness = Blueprint("freshness", __name__, url_prefix="/admin/freshness")

# Enough pages to cover a handbook's front matter, the deans roster and the
# signature blocks, without holding an entire 200-page scan in memory during what
# is meant to be a fast interactive check.
MAX_SCAN_PAGES = 60


def _require_admin() -> bool:
    return bool(session.get("user")) and is_admin()


def _load_pages(path: str):
    """
    Read a PDF into `(text, page_number)` pairs.

    Imported lazily: this module is imported at app start, and pulling the PDF
    stack in at that point would slow every boot — including the ones that never
    upload anything.
    """
    from langchain_community.document_loaders import PyPDFLoader

    docs = PyPDFLoader(path).load()
    out = []
    for d in docs[:MAX_SCAN_PAGES]:
        md = getattr(d, "metadata", None) or {}
        page = md.get("page")
        # PyPDF pages are 0-based; every other screen in this project cites the
        # page number a human would read off the document.
        out.append((getattr(d, "page_content", "") or "",
                    (page + 1) if isinstance(page, int) else page))
    return out


@bp_freshness.route("/api/list")
def api_list():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    docs = list_docs()
    return jsonify({
        "success": True,
        "documents": docs,
        "stats": {
            "total": len(docs),
            # The count an admin should act on: an undated document can neither
            # win nor lose a freshness comparison, so it will keep contradicting
            # its neighbours silently.
            "undated": sum(1 for d in docs if not d.get("effective_date")),
            "low_confidence": sum(1 for d in docs if d.get("confidence") == "low"),
        },
    })


@bp_freshness.route("/api/set-date", methods=["POST"])
def api_set_date():
    """
    Record or correct a document's effective date.

    An empty `effective_date` is meaningful: it re-runs inference from the
    filename, which is how an admin undoes a wrong manual date without having to
    guess what the automatic answer would have been.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"success": False, "message": "A filename is required."}), 400

    try:
        rec = set_doc_date(
            filename,
            effective_date=(data.get("effective_date") or "").strip(),
            text=data.get("text") or "",
            set_by=session.get("user", ""),
            note=data.get("note") or "",
        )
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    return jsonify({"success": True, "document": rec})


@bp_freshness.route("/api/delete", methods=["POST"])
def api_delete():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"success": False, "message": "A filename is required."}), 400

    if not delete_doc(filename):
        return jsonify({"success": False, "message": "No date on record for that file."}), 404
    return jsonify({"success": True})


@bp_freshness.route("/api/scan-upload", methods=["POST"])
def api_scan_upload():
    """
    Dry-run an upload: what would this file contradict?

    Nothing is indexed, nothing is uploaded to S3, and no date is recorded. The
    response is a report the admin reads before committing.

    `existing_text` (optional) lets the caller supply the currently-indexed text
    to compare against. It is a parameter rather than a Pinecone query on purpose:
    a data-quality check that needs the vector store to be reachable is a check
    that stops working exactly when the corpus is in trouble.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    if "file" not in request.files:
        return jsonify({"success": False, "message": "No file was sent."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"success": False, "message": "No file was sent."}), 400

    stated = (request.form.get("effective_date") or "").strip()

    # The existing corpus, as text. Absent means "compare against nothing", which
    # still yields the incoming file's date and its fact list — useful on a first
    # upload, and honest about having nothing to compare against.
    existing_facts = []
    existing_text = request.form.get("existing_text") or ""
    existing_source = (request.form.get("existing_source") or "").strip()
    if existing_text:
        existing_facts = extract_facts(existing_text, source=existing_source)

    suffix = os.path.splitext(f.filename)[1]
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name

        pages = _load_pages(tmp_path)
        report = preview_upload(f.filename, pages, existing_facts,
                                stated_date=stated)
        report["pages_scanned"] = len(pages)
        return jsonify({"success": True, "report": report})
    except Exception as e:
        return jsonify({"success": False,
                        "message": f"Could not scan {f.filename}: {e}"}), 500
    finally:
        # The temp file must go even when the scan blew up half way through it;
        # otherwise a malformed PDF leaves a copy of a college document on disk.
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@bp_freshness.route("/api/preview")
def api_preview():
    """
    Show the exact `<document_dates>` block the model would receive for a given
    set of sources, so an admin can confirm the dates they set actually reach the
    answer instead of taking it on faith.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    raw = request.args.get("sources") or ""
    sources = [s.strip() for s in raw.split(",") if s.strip()]
    block = freshness_block(*sources)
    return jsonify({
        "success": True,
        "applies": bool(block),
        "block": block,
        # Spelled out, because an empty block has two very different causes and an
        # admin staring at a blank box cannot tell them apart.
        "explanation": (
            "The model is told which document is newer."
            if block else
            "No block is sent: fewer than two of these sources have a known, "
            "different effective date, so there is nothing for the model to "
            "prefer."
        ),
    })
