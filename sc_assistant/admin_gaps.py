"""
sc_assistant/admin_gaps.py — admin API for the Content Gaps screen.

Where Data Conflicts answers "which of these two values is right?", this answers
the other half of the same problem: "what are students asking that our documents
cannot answer at all?"

Endpoints
---------
GET  /admin/gaps/api/list?status=open   -> ranked topics + headline stats
POST /admin/gaps/api/resolve            -> mark a topic covered (doc uploaded)
POST /admin/gaps/api/reopen             -> undo that
POST /admin/gaps/api/delete             -> drop a row (spam / test question)

No scanning here: the rows are produced by real chat traffic in
`sc_assistant/chat.py`, so the list is already the truth about what students
want. Reads are cheap (one small JSON file), so there is no cache to invalidate.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from .utils import is_admin
from rag.gaps import (
    list_gaps,
    gap_stats,
    set_gap_status,
    delete_gap,
)

bp_gaps = Blueprint("gaps", __name__, url_prefix="/admin/gaps")


def _require_admin() -> bool:
    return bool(session.get("user")) and is_admin()


@bp_gaps.route("/api/list")
def api_list():
    """
    Ranked list of unanswered topics, most-asked first.

    `?status=open|resolved` filters; omitting it returns everything so the UI can
    show resolved history without a second request.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    status = request.args.get("status") or None
    if status not in (None, "open", "resolved"):
        return jsonify({"success": False, "message": "Invalid status filter."}), 400

    try:
        return jsonify({
            "success": True,
            "gaps": list_gaps(status),
            "stats": gap_stats(),
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not read gaps: {e}"}), 500


@bp_gaps.route("/api/resolve", methods=["POST"])
def api_resolve():
    """
    Body: {key, note}

    "Resolved" means the admin has uploaded or edited a document that covers the
    topic. It is not a silencer: if the same topic is asked again, `record_gap()`
    reopens the row automatically, which is how a fix that did not actually work
    becomes visible instead of staying hidden.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"success": False, "message": "Missing topic key."}), 400

    entry = set_gap_status(
        key,
        "resolved",
        resolved_by=session.get("user", ""),
        note=(data.get("note") or "").strip(),
    )
    if not entry:
        return jsonify({"success": False, "message": "That topic no longer exists."}), 404

    return jsonify({
        "success": True,
        "message": "Marked as covered. It will reopen automatically if students ask again.",
        "gap": entry,
        "stats": gap_stats(),
    })


@bp_gaps.route("/api/reopen", methods=["POST"])
def api_reopen():
    """Body: {key} — put a topic back in the work queue."""
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    key = ((request.get_json(silent=True) or {}).get("key") or "").strip()
    if not key:
        return jsonify({"success": False, "message": "Missing topic key."}), 400

    entry = set_gap_status(key, "open")
    if not entry:
        return jsonify({"success": False, "message": "That topic no longer exists."}), 404

    return jsonify({
        "success": True,
        "message": "Reopened.",
        "gap": entry,
        "stats": gap_stats(),
    })


@bp_gaps.route("/api/delete", methods=["POST"])
def api_delete():
    """
    Body: {key} — remove a row entirely.

    For junk: test questions during development, or someone asking the bot about
    the weather. Deleting keeps the queue meaningful; if a real student asks it
    again it simply comes back.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    key = ((request.get_json(silent=True) or {}).get("key") or "").strip()
    if not key:
        return jsonify({"success": False, "message": "Missing topic key."}), 400

    if not delete_gap(key):
        return jsonify({"success": False, "message": "That topic no longer exists."}), 404

    return jsonify({"success": True, "message": "Removed.", "stats": gap_stats()})
