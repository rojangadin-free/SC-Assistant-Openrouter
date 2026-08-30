"""
sc_assistant/admin_feedback.py — admin API for Answer Quality and the
Ask-a-Human queue.

Two related screens, one blueprint, because they answer the same question from
opposite ends:

    Answer Quality   "which answers are students unhappy with, and which
                      documents keep showing up underneath them?"
    Escalations      "which students are still waiting for a human reply?"

Endpoints
---------
GET  /admin/feedback/api/list?verdict=down   -> topics, worst first, + stats
POST /admin/feedback/api/delete              -> drop a topic (test traffic)
GET  /admin/feedback/api/regression          -> approved Q/A as a test set

GET  /admin/feedback/api/escalations?status=pending
POST /admin/feedback/api/escalations/answer  -> record the human reply
POST /admin/feedback/api/escalations/status  -> dismiss / reopen
POST /admin/feedback/api/escalations/delete
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from .utils import is_admin
from rag.feedback import (
    list_topics,
    feedback_stats,
    delete_topic,
    regression_questions,
)
from rag.escalation import (
    list_escalations,
    escalation_stats,
    answer_escalation,
    set_status,
    delete_escalation,
    ROUTES,
    STATUSES,
)

bp_feedback = Blueprint("feedback", __name__, url_prefix="/admin/feedback")


def _require_admin() -> bool:
    return bool(session.get("user")) and is_admin()


# ============================================================
# ANSWER QUALITY
# ============================================================

@bp_feedback.route("/api/list")
def api_list():
    """
    Topics ranked **worst first**.

    `?verdict=down` shows only topics with complaints; `?verdict=up` shows the
    clean ones (useful when picking regression cases). Sorting by failure rather
    than by popularity is deliberate: a list sorted by volume tells you what is
    busy, not what is broken.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    verdict = request.args.get("verdict") or None
    if verdict not in (None, "up", "down"):
        return jsonify({"success": False, "message": "Invalid verdict filter."}), 400

    try:
        return jsonify({
            "success": True,
            "topics": list_topics(verdict),
            "stats": feedback_stats(),
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not read feedback: {e}"}), 500


@bp_feedback.route("/api/delete", methods=["POST"])
def api_delete():
    """Body: {key} — remove a topic and its votes (development/test traffic)."""
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    key = ((request.get_json(silent=True) or {}).get("key") or "").strip()
    if not key:
        return jsonify({"success": False, "message": "Missing topic key."}), 400

    if not delete_topic(key):
        return jsonify({"success": False, "message": "That topic no longer exists."}), 404

    return jsonify({"success": True, "message": "Removed.", "stats": feedback_stats()})


@bp_feedback.route("/api/regression")
def api_regression():
    """
    Questions students approved, with the documents that answered them.

    Exported rather than displayed: this is meant to be fed to
    `eval_retrieval.py` so that after a chunking or reranker change you can prove
    the answers that used to work still work, instead of sampling by hand.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        min_up = int(request.args.get("min_upvotes", 1))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "min_upvotes must be a number."}), 400

    cases = regression_questions(min_upvotes=min_up)
    return jsonify({"success": True, "count": len(cases), "cases": cases})


# ============================================================
# ESCALATIONS ("Ask a human")
# ============================================================

@bp_feedback.route("/api/escalations")
def api_escalations():
    """Pending first, oldest first — the person waiting longest is at the top."""
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    status = request.args.get("status") or None
    route = request.args.get("route") or None
    if status not in (None,) + STATUSES:
        return jsonify({"success": False, "message": "Invalid status filter."}), 400
    if route is not None and route not in ROUTES:
        return jsonify({"success": False, "message": "Unknown office."}), 400

    return jsonify({
        "success": True,
        "escalations": list_escalations(status, route),
        "stats": escalation_stats(),
        "routes": ROUTES,
    })


@bp_feedback.route("/api/escalations/answer", methods=["POST"])
def api_escalation_answer():
    """
    Body: {id, reply}

    The reply text is stored, not just marked done. It is the raw material for
    the document that should have answered this in the first place — and it pairs
    with the Content Gaps row for the same topic, so "answer the student" and
    "fix the corpus" are one workflow instead of two.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    esc_id = (data.get("id") or "").strip()
    reply = (data.get("reply") or "").strip()

    if not esc_id:
        return jsonify({"success": False, "message": "Missing escalation id."}), 400
    if not reply:
        return jsonify({"success": False, "message": "A reply is required."}), 400

    item = answer_escalation(esc_id, reply, answered_by=session.get("user", ""))
    if not item:
        return jsonify({"success": False, "message": "That request no longer exists."}), 404

    return jsonify({
        "success": True,
        "message": f"Answered. Reply to {item['reply_to']}.",
        "escalation": item,
        "stats": escalation_stats(),
    })


@bp_feedback.route("/api/escalations/status", methods=["POST"])
def api_escalation_status():
    """Body: {id, status} — dismiss spam, or put a row back in the queue."""
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    esc_id = (data.get("id") or "").strip()
    status = (data.get("status") or "").strip().lower()

    if not esc_id:
        return jsonify({"success": False, "message": "Missing escalation id."}), 400
    if status not in STATUSES:
        return jsonify({
            "success": False,
            "message": f"Status must be one of: {', '.join(STATUSES)}.",
        }), 400

    item = set_status(esc_id, status)
    if not item:
        return jsonify({"success": False, "message": "That request no longer exists."}), 404

    return jsonify({
        "success": True,
        "message": f"Marked {status}.",
        "escalation": item,
        "stats": escalation_stats(),
    })


@bp_feedback.route("/api/escalations/delete", methods=["POST"])
def api_escalation_delete():
    """Body: {id}"""
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    esc_id = ((request.get_json(silent=True) or {}).get("id") or "").strip()
    if not esc_id:
        return jsonify({"success": False, "message": "Missing escalation id."}), 400

    if not delete_escalation(esc_id):
        return jsonify({"success": False, "message": "That request no longer exists."}), 404

    return jsonify({"success": True, "message": "Removed.", "stats": escalation_stats()})
