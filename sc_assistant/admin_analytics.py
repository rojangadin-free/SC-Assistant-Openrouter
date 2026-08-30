"""
sc_assistant/admin_analytics.py — admin API for the Analytics screen.

Endpoints
---------
GET /admin/analytics/api/overview?days=14   everything, in one payload
GET /admin/analytics/api/topics?kind=&limit= drill-down on what students ask
GET /admin/analytics/api/documents          pages that keep producing bad answers

One aggregate endpoint rather than six small ones. These numbers are read
together and quoted against each other ("we answered 78% of 214 questions"), so
they have to come from a single snapshot: six requests can interleave with a
student's vote and render a page whose headline and chart disagree by one, which
is exactly the kind of off-by-one that makes an admin distrust every other number
on the screen.

Read-only by design. There is no POST here and there should never be one — every
figure is derived from the feedback, gap and escalation stores, so the only honest
way to change a number on this page is to change what actually happened. An
endpoint that could edit a metric would turn the dashboard from evidence into
decoration.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from .utils import is_admin
from rag.analytics import (
    DEFAULT_DAYS,
    overview,
    top_topics,
    suspect_documents,
)

bp_analytics = Blueprint("analytics", __name__, url_prefix="/admin/analytics")

# A window longer than a school year is not a trend, it is a history lesson — and
# it makes the day-by-day series long enough to be slow to render for no gain.
MAX_DAYS = 365


def _require_admin() -> bool:
    return bool(session.get("user")) and is_admin()


def _days_arg() -> int:
    """
    Parse `?days=`, clamped.

    Clamped rather than rejected: a stray `days=99999` from a bookmarked URL
    should show a year of data, not a 400. But it is clamped rather than ignored,
    because an unbounded value means an unbounded response, and this endpoint is
    reachable by anyone with an admin session.
    """
    raw = request.args.get("days")
    if not raw:
        return DEFAULT_DAYS
    try:
        return max(1, min(MAX_DAYS, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


@bp_analytics.route("/api/overview")
def api_overview():
    """
    Headline totals, the day-by-day series, the hourly histogram, top topics,
    suspect documents, escalation response times, and week-over-week deltas.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        return jsonify({"success": True, "analytics": overview(_days_arg())})
    except Exception as e:
        # A failed chart must read as a failed chart. Returning zeros here would
        # be worse than an error: "0 questions, 0% answered" is a plausible-looking
        # answer that an admin could act on.
        return jsonify({"success": False, "message": f"Could not compute analytics: {e}"}), 500


@bp_analytics.route("/api/topics")
def api_topics():
    """
    `?kind=unanswered` is the document roadmap: the questions students ask most
    that the corpus cannot answer, ranked. `?kind=answered` is its mirror, useful
    for deciding which topics deserve a guided flow.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    kind = request.args.get("kind") or None
    if kind not in (None, "answered", "unanswered", "escalated"):
        return jsonify({"success": False, "message": "Invalid kind filter."}), 400

    try:
        limit = max(1, min(100, int(request.args.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20

    try:
        return jsonify({"success": True, "topics": top_topics(limit, kind=kind)})
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not read topics: {e}"}), 500


@bp_analytics.route("/api/documents")
def api_documents():
    """
    Which source pages sit under downvoted answers, worst first.

    This is the actionable end of the feedback loop: a 👎 says an answer was bad,
    but the sources stored with the vote say which page produced it — so this list
    tells an admin exactly which page to re-read, correct, or re-chunk.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        limit = max(1, min(100, int(request.args.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20

    try:
        return jsonify({"success": True, "documents": suspect_documents(limit)})
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not read documents: {e}"}), 500
