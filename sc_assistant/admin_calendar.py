"""
sc_assistant/admin_calendar.py — admin API for the Academic Calendar screen.

    GET    /admin/calendar/api/list     -> periods + today's computed state
    POST   /admin/calendar/api/save     -> add or correct one period
    POST   /admin/calendar/api/delete   -> remove one period
    POST   /admin/calendar/api/scan     -> propose periods found in the documents
    POST   /admin/calendar/api/preview  -> what the assistant would be told, for
                                           a question and (optionally) a date

Why `preview` exists
--------------------
Every other admin screen in this project shows the admin their *input*. This one
shows the **output**: the exact `<calendar>` text the model receives. Timing bugs
are invisible otherwise — a wrong end date looks perfectly reasonable in a form
and only becomes visible as "closes in 40 days" inside an answer.

It also accepts an arbitrary `today`, which turns "will this still be right in
October?" from a question you wait three months to answer into one you check in a
second.
"""

from __future__ import annotations

import datetime

from flask import Blueprint, jsonify, request, session

from .utils import is_admin
from rag.calendar import (
    calendar_block,
    current_state,
    delete_period,
    list_periods,
    parse_periods,
    set_period,
    today_in_manila,
)

bp_calendar = Blueprint("calendar", __name__, url_prefix="/admin/calendar")


def _require_admin() -> bool:
    return bool(session.get("user")) and is_admin()


def _parse_today(raw: str):
    """
    Read an optional `today` override. Returns (date, error_message).

    A bad date is rejected rather than silently falling back to the real today:
    an admin testing "October 1" and being shown today's state instead would
    conclude the calendar is broken.
    """
    raw = (raw or "").strip()
    if not raw:
        return today_in_manila(), None
    try:
        return datetime.date.fromisoformat(raw), None
    except ValueError:
        return None, "Date must be YYYY-MM-DD."


@bp_calendar.route("/api/list")
def api_list():
    """Admin-entered periods, plus how they classify against a date."""
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    today, err = _parse_today(request.args.get("today", ""))
    if err:
        return jsonify({"success": False, "message": err}), 400

    periods = list_periods()
    state = current_state(periods, today)
    return jsonify({
        "success": True,
        "periods": periods,
        "state": state,
        "stats": {
            "total": len(periods),
            "active": len(state["active"]),
            "upcoming": len(state["upcoming"]),
            "past": len(state["past"]),
        },
    })


@bp_calendar.route("/api/save", methods=["POST"])
def api_save():
    """
    Body: {label, start, end, note}

    Re-saving an existing label corrects it in place. That is the whole point:
    two rows for "Enrollment" would recreate, inside this feature, exactly the
    contradiction that `rag/conflicts.py` exists to remove.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    start = (data.get("start") or "").strip()
    end = (data.get("end") or "").strip()

    if not label:
        return jsonify({"success": False, "message": "Event name is required."}), 400
    if not start:
        return jsonify({"success": False, "message": "Start date is required."}), 400

    # `note` is only forwarded when the client actually sent the field. Coercing a
    # missing key to "" would wipe an existing note every time someone corrected
    # a date — see the same distinction in `rag.calendar.set_period`.
    note = data.get("note")
    period = set_period(
        label,
        start,
        end,
        note=note.strip() if isinstance(note, str) else None,
        set_by=session.get("user", ""),
    )

    if not period:
        return jsonify({
            "success": False,
            "message": "Invalid dates. Use YYYY-MM-DD, and make sure the end is not before the start.",
        }), 400

    return jsonify({
        "success": True,
        "message": f'Saved "{period["label"]}".',
        "period": period,
        "periods": list_periods(),
    })


@bp_calendar.route("/api/delete", methods=["POST"])
def api_delete():
    """Body: {key}"""
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    key = ((request.get_json(silent=True) or {}).get("key") or "").strip()
    if not key:
        return jsonify({"success": False, "message": "Missing key."}), 400

    if not delete_period(key):
        return jsonify({"success": False, "message": "That period no longer exists."}), 404

    return jsonify({"success": True, "message": "Deleted.", "periods": list_periods()})


@bp_calendar.route("/api/scan", methods=["POST"])
def api_scan():
    """
    Body: {text} — propose periods found in pasted document text.

    Deliberately a paste box rather than an S3 sweep like the conflicts scanner.
    Dates appear on nearly every page of a college handbook (founding years,
    accreditation dates, sample computations), so a whole-corpus scan returns
    hundreds of rows that are technically dates and not one of them a deadline.
    Pasting the calendar page is a two-second action that removes all of that
    noise, and the admin still confirms every row before it is saved.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    if not text.strip():
        return jsonify({"success": False, "message": "Paste the calendar text first."}), 400

    year = data.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None

    found = parse_periods(text, default_year=year)

    # Flag what is already known so the UI can grey it out instead of inviting the
    # admin to re-save rows they have already confirmed. Matching is on the dates,
    # not the label: a parsed label carries whatever wording the document used
    # ("Enrollment Period") while the stored one carries what the admin typed
    # ("Enrollment"), so comparing labels would report every confirmed row as new.
    # The label is still accepted as a match, for a period whose dates were later
    # corrected here.
    existing = list_periods()
    existing_keys = {p["key"] for p in existing}
    existing_spans = {(p.get("start"), p.get("end")) for p in existing}
    for f in found:
        f["already_saved"] = (
            f["label"].lower() in existing_keys
            or (f["start"], f["end"]) in existing_spans
        )


    return jsonify({
        "success": True,
        "found": found,
        "message": f"Found {len(found)} date range(s). Review and save the ones that are deadlines.",
    })


@bp_calendar.route("/api/preview", methods=["POST"])
def api_preview():
    """
    Body: {question, today}

    Returns the literal `<calendar>` block the assistant would receive. An empty
    string is a valid, meaningful answer: it means the question was not about
    timing, and the admin should see that rather than assume the feature is
    broken.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"success": False, "message": "Type a question to preview."}), 400

    today, err = _parse_today(data.get("today", ""))
    if err:
        return jsonify({"success": False, "message": err}), 400

    block = calendar_block(question, today=today)
    return jsonify({
        "success": True,
        "question": question,
        "today": today.isoformat(),
        "block": block,
        "applies": bool(block),
        "message": (
            "This is exactly what the assistant is told about timing."
            if block else
            "No calendar context — this question was not detected as time-related, "
            "so the assistant answers from the documents alone."
        ),
    })
