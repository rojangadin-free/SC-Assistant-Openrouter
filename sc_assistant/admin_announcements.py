"""
sc_assistant/admin_announcements.py — admin API for the Announcements screen,
plus the one public endpoint the chat page calls to draw its banner.

    GET    /admin/announcements/api/list      -> every announcement + stats
    POST   /admin/announcements/api/save      -> post or edit one
    POST   /admin/announcements/api/delete    -> remove one
    POST   /admin/announcements/api/toggle    -> take down / put back
    POST   /admin/announcements/api/preview   -> the <announcements> block the model gets

    GET    /api/announcements                 -> PUBLIC: what to pin for this viewer

Why one route is public
-----------------------
The banner has to render for guests. A prospective student checking "is the
campus open today?" during a typhoon is exactly who the notice is for, and they
have no account. The public endpoint therefore returns only what is already meant
to be broadcast — title, body, priority, dates — and never `posted_by`, which is
staff information that has no reason to leave the admin screen.
"""

from __future__ import annotations

import datetime
import hashlib

from flask import Blueprint, jsonify, request, session


from .utils import is_admin
from rag.announcements import (
    AUDIENCES,
    PRIORITIES,
    announcement_block,
    delete_announcement,
    is_expired,
    list_announcements,
    pinned_announcements,
    save_announcement,
    set_active,
    stats,
)
from rag.calendar import today_in_manila

bp_announcements = Blueprint("announcements", __name__)


def _require_admin() -> bool:
    return bool(session.get("user")) and is_admin()


def _parse_today(raw: str):
    """Optional `today` override, rejected rather than ignored when malformed —
    same reasoning as the calendar screen: silently showing the real today would
    make the admin think the feature is broken."""
    raw = (raw or "").strip()
    if not raw:
        return today_in_manila(), None
    try:
        return datetime.date.fromisoformat(raw), None
    except ValueError:
        return None, "Date must be YYYY-MM-DD."


def _public_view(a: dict) -> dict:
    """The subset of a record that is safe to hand to any viewer."""
    return {
        "key": a.get("key", ""),
        "title": a.get("title", ""),
        "body": a.get("body", ""),
        "priority": a.get("priority", "info"),
        "starts_on": a.get("starts_on", ""),
        "expires_on": a.get("expires_on", ""),
    }


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

@bp_announcements.route("/api/announcements")
def api_public():
    """
    What the chat page pins, for whoever is looking at it.

    Audience is derived server-side from the session. Taking it from a query
    parameter would let any student read faculty-only notices by editing a URL.
    """
    audience = "faculty" if (session.get("user") and is_admin()) else "students"
    items = pinned_announcements(audience=audience)

    # `viewer` scopes the browser's dismissal memory. Dismissals are stored in
    # localStorage, which is per-browser, not per-account — so on a shared campus
    # PC one student dismissing a suspension notice used to hide it from the next
    # person to log in on that machine. That is the one failure this feature
    # cannot afford. Sending an opaque viewer tag lets the client namespace its
    # keys per account; an email is not used because it would put addresses in
    # localStorage for the next user to read.
    who = session.get("uid") or session.get("user") or ""
    viewer = hashlib.sha256(who.encode("utf-8")).hexdigest()[:16] if who else "guest"

    return jsonify({
        "success": True,
        "announcements": [_public_view(a) for a in items],
        "count": len(items),
        "viewer": viewer,
    })



# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@bp_announcements.route("/admin/announcements/api/list")
def api_list():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    today, err = _parse_today(request.args.get("today", ""))
    if err:
        return jsonify({"success": False, "message": err}), 400

    items = []
    for a in list_announcements():
        row = dict(a)
        # Computed here rather than in the browser: "is this still live" depends
        # on campus time, and the admin's laptop clock is not campus time.
        row["expired"] = is_expired(a, today)
        row["live"] = (
            not row["expired"]
            and a.get("active", True)
            and not (
                a.get("starts_on")
                and datetime.date.fromisoformat(a["starts_on"]) > today
            )
        )
        items.append(row)

    return jsonify({
        "success": True,
        "announcements": items,
        "stats": stats(today=today),
        "priorities": list(PRIORITIES),
        "audiences": AUDIENCES,
        "today": today.isoformat(),
    })


@bp_announcements.route("/admin/announcements/api/save", methods=["POST"])
def api_save():
    """Body: {key?, title, body, priority, audience, starts_on, expires_on, pinned}"""
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"success": False, "message": "A headline is required."}), 400

    saved = save_announcement(
        title,
        data.get("body") or "",
        key=(data.get("key") or "").strip(),
        priority=(data.get("priority") or "info").strip(),
        audience=(data.get("audience") or "all").strip(),
        starts_on=(data.get("starts_on") or "").strip(),
        expires_on=(data.get("expires_on") or "").strip(),
        pinned=bool(data.get("pinned", True)),
        posted_by=session.get("user", ""),
    )

    if not saved:
        return jsonify({
            "success": False,
            "message": "Check the dates: use YYYY-MM-DD, and the end cannot be before the start.",
        }), 400

    return jsonify({
        "success": True,
        "message": f'Posted "{saved["title"]}".',
        "announcement": saved,
    })


@bp_announcements.route("/admin/announcements/api/delete", methods=["POST"])
def api_delete():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    key = ((request.get_json(silent=True) or {}).get("key") or "").strip()
    if not key:
        return jsonify({"success": False, "message": "Missing key."}), 400

    if not delete_announcement(key):
        return jsonify({"success": False, "message": "That announcement no longer exists."}), 404

    return jsonify({"success": True, "message": "Deleted."})


@bp_announcements.route("/admin/announcements/api/toggle", methods=["POST"])
def api_toggle():
    """
    Body: {key, active}

    The fast path during an emergency: a suspension that gets lifted needs to
    stop reaching students immediately, and "delete the evidence" is the wrong
    way to do that.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"success": False, "message": "Missing key."}), 400
    if "active" not in data:
        return jsonify({"success": False, "message": "Missing active flag."}), 400

    updated = set_active(key, bool(data.get("active")))
    if not updated:
        return jsonify({"success": False, "message": "That announcement no longer exists."}), 404

    return jsonify({
        "success": True,
        "message": "Now showing." if updated.get("active") else "Taken down.",
        "announcement": updated,
    })


@bp_announcements.route("/admin/announcements/api/preview", methods=["POST"])
def api_preview():
    """
    Body: {audience?, today?}

    The literal text the assistant is given. Same argument as the calendar's
    preview: an announcement that reads perfectly in a form can still fail to
    reach the model — wrong audience, start date in the future, already expired —
    and this is the only place that difference is visible.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    audience = (data.get("audience") or "students").strip()
    if audience not in AUDIENCES:
        return jsonify({"success": False, "message": "Unknown audience."}), 400

    today, err = _parse_today(data.get("today", ""))
    if err:
        return jsonify({"success": False, "message": err}), 400

    block = announcement_block(audience=audience, today=today)
    return jsonify({
        "success": True,
        "audience": audience,
        "today": today.isoformat(),
        "block": block,
        "applies": bool(block),
        "message": (
            "This is exactly what the assistant is told."
            if block else
            "Nothing is active for this audience on this date, so the assistant "
            "answers from the documents alone."
        ),
    })
