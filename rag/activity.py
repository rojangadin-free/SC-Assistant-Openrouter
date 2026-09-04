"""
rag/activity.py — an accurate "what just happened" feed, and the triage counts
that belong on the landing screen.

Why this file exists
--------------------
The Overview screen had a Recent Activities list that was wrong, and wrong in the
most expensive way: it looked right. It was built like this:

    files_table.scan(Limit=5)          # "the 5 newest documents"
    conversations_table.scan(Limit=5)  # "the 5 newest conversations"
    ...then sort all of them by timestamp and show the top 5

`Limit` on a DynamoDB scan is not "newest 5". A scan has no order at all — it
returns whatever it finds first, and `Limit` just stops it early. So the sort was
ordering an arbitrary handful of rows correctly and calling that a timeline. A
document uploaded thirty seconds ago appeared only if it happened to sit in the
first five items the scan walked past; once the table held more than a handful of
rows, the "recent" list could be months old and never change.

That is the whole complaint ("recent activity is not being updated accurately"),
and it cannot be fixed by sorting harder. Either the query has to return the
newest rows, or the timeline has to be built from data that carries usable
timestamps.

What this module does instead
-----------------------------
It builds the feed from the six stores that already record *when* something
happened, in Manila time:

    rag/feedback.py     a student judged an answer          `at`
    rag/gaps.py         a question the documents missed     `examples[].at`
    rag/escalation.py   raised, and separately answered     `created_at`/`answered_at`
    rag/conflicts.py    an admin adjudicated a contradiction `resolved_at`
    rag/calendar.py     an admin set a date range           `set_at`
    rag/freshness.py    an admin dated a document           `updated_at`

These are small JSON documents read whole, so "newest" is a real sort over the
whole set rather than a sort over a lucky subset. `rag/analytics.py` already
proved this shape works — it flattens four of these into one timeline for the
charts. This module is deliberately NOT built on `analytics._events()` even so:
that function keeps only what a metric needs (date, hour, topic, kind) and drops
the human detail a feed exists to show, and widening it would change every number
on the Analytics screen. Two readers, one storage layer.

DynamoDB rows (uploads, new accounts) are merged in by the caller in
`sc_assistant/admin.py`, which owns the boto3 clients. This module stays
import-safe without AWS credentials so it can be tested offline.

Everything degrades to an empty list rather than raising. This is a reporting
surface; a broken feed must never take down the dashboard, let alone the
assistant.
"""

from __future__ import annotations

import datetime
from typing import Dict, List, Optional

from rag.calendar import MANILA

# Read the stores through the same private loaders analytics.py uses. Going
# through the public list_*() helpers instead would re-sort and re-shape data
# this module immediately re-sorts anyway, and some of them hide the very
# timestamps needed here.
from rag.feedback import _load as _load_feedback, feedback_stats
from rag.gaps import _load as _load_gaps, gap_stats
from rag.escalation import _load as _load_escalations, escalation_stats


# How many rows the feed shows by default. Ten is about one screen without
# scrolling; a feed long enough to scroll stops being glanceable, which was the
# only thing it was ever good for.
DEFAULT_LIMIT = 10

# Nothing older than this appears. A feed whose newest entry is from March is
# not a feed, and showing it implies the system has been idle rather than that
# the window is simply quiet — so an explicit "nothing in the last N days" is
# more honest than three stale rows.
DEFAULT_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _parse(ts: str) -> Optional[datetime.datetime]:
    """
    ISO-8601 -> aware datetime in Manila time, or None.

    Same contract as `analytics._parse`, duplicated rather than imported to keep
    this module independent of the metrics code: a change made for a chart axis
    should not silently reorder the feed.
    """
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(MANILA)
    except Exception:
        return None


def humanize(ts: str, *, now: Optional[datetime.datetime] = None) -> str:
    """
    "just now", "5 minutes ago", "3 hours ago", "yesterday", "12 Aug".

    Switches to an absolute date after a week. "23 days ago" is a number the
    reader has to do arithmetic on to place; a date is not.
    """
    dt = _parse(ts)
    if not dt:
        return ""
    now = now or datetime.datetime.now(MANILA)
    seconds = (now - dt).total_seconds()

    # A clock skew between the writer and the reader can put an event a few
    # seconds into the future. "in 3 seconds" on an activity feed reads as a bug,
    # so anything ahead of now is just "just now".
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        n = int(seconds // 60)
        return f"{n} minute{'s' if n > 1 else ''} ago"
    if seconds < 86400:
        n = int(seconds // 3600)
        return f"{n} hour{'s' if n > 1 else ''} ago"
    if seconds < 172800:
        return "yesterday"
    if seconds < 604800:
        return f"{int(seconds // 86400)} days ago"
    # %-d is not portable to Windows, and this runs on both.
    return f"{dt.day} {dt.strftime('%b')}"


def _clip(text: str, limit: int = 90) -> str:
    """
    One line, ellipsised. Questions arrive with newlines from a textarea, and a
    raw newline inside a feed row breaks the layout it sits in.
    """
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------


def _event(at: datetime.datetime, *, icon: str, tone: str, text: str,
           section: str = "") -> dict:
    """
    One row. `section` is the dashboard section this row is *about*, so the UI can
    make it clickable — a feed that reports a problem without offering the screen
    that fixes it just makes the reader hunt for it.
    """
    return {
        "at": at.isoformat(),
        "_sort": at,
        "icon": icon,
        "tone": tone,
        "text": text,
        "section": section,
        "time": humanize(at.isoformat()),
    }


def events(*, window_days: int = DEFAULT_WINDOW_DAYS) -> List[dict]:
    """
    Every dateable thing that happened in the window, newest first.

    Each store is read in its own try/except: one unreadable file costs its own
    rows, not the whole feed.
    """
    out: List[dict] = []
    cutoff = datetime.datetime.now(MANILA) - datetime.timedelta(days=window_days)

    # --- feedback: a student judged an answer ---------------------------
    try:
        for v in (_load_feedback().get("votes") or {}).values():
            dt = _parse(v.get("at", ""))
            if not dt or dt < cutoff:
                continue
            up = (v.get("verdict") or "") == "up"
            out.append(_event(
                dt,
                icon="fa-thumbs-up" if up else "fa-thumbs-down",
                tone="ok" if up else "bad",
                text=("Answer rated helpful" if up else "Answer marked unhelpful")
                     + (f": <strong>{_clip(v.get('question', ''), 70)}</strong>"
                        if v.get("question") else ""),
                section="feedback",
            ))
    except Exception as e:
        print(f"  [activity] feedback unreadable (non-fatal): {e}")

    # --- gaps: the documents could not answer something -----------------
    #
    # One row per recorded example, not per group: `count` on a group is a
    # running total with no timestamp, so it cannot be placed on a timeline. Same
    # trade-off analytics makes, for the same reason.
    try:
        for g in (_load_gaps().get("gaps") or {}).values():
            for ex in (g.get("examples") or []):
                dt = _parse(ex.get("at", ""))
                if not dt or dt < cutoff:
                    continue
                out.append(_event(
                    dt,
                    icon="fa-circle-question",
                    tone="warn",
                    text="Could not answer: <strong>"
                         + _clip(ex.get("question") or g.get("topic", ""), 70)
                         + "</strong>",
                    section="gaps",
                ))
    except Exception as e:
        print(f"  [activity] gaps unreadable (non-fatal): {e}")

    # --- escalations: raised, and answered ------------------------------
    try:
        for item in (_load_escalations().get("items") or {}).values():
            raised = _parse(item.get("created_at", ""))
            if raised and raised >= cutoff:
                out.append(_event(
                    raised,
                    icon="fa-hand",
                    tone="warn",
                    text=f"Question sent to {item.get('route_label') or 'an office'}"
                         f": <strong>{_clip(item.get('question', ''), 60)}</strong>",
                    section="escalations",
                ))
            # The reply is its own event at its own time. Collapsing the two into
            # one row would lose the only fact worth knowing here — how long the
            # student waited.
            answered = _parse(item.get("answered_at", ""))
            if answered and answered >= cutoff:
                who = item.get("answered_by") or "an admin"
                out.append(_event(
                    answered,
                    icon="fa-reply",
                    tone="ok",
                    text=f"<strong>{who}</strong> replied to a student question",
                    section="escalations",
                ))
    except Exception as e:
        print(f"  [activity] escalations unreadable (non-fatal): {e}")

    # --- conflicts: an admin picked the correct value --------------------
    try:
        from rag.conflicts import list_resolutions
        for r in (list_resolutions() or {}).values():
            dt = _parse(r.get("resolved_at", ""))
            if not dt or dt < cutoff:
                continue
            role = (r.get("role") or "").replace("_", " ").title()
            subject = (r.get("subject") or "").upper()
            label = f"{role} of {subject}".strip() if role or subject else "A conflict"
            out.append(_event(
                dt,
                icon="fa-scale-balanced",
                tone="ok",
                text=f"{label} confirmed as "
                     f"<strong>{_clip(r.get('correct', ''), 40)}</strong>",
                section="conflicts",
            ))
    except Exception as e:
        print(f"  [activity] conflicts unreadable (non-fatal): {e}")

    # --- calendar: an admin set a date range ----------------------------
    try:
        from rag.calendar import list_periods
        for p in (list_periods() or []):
            dt = _parse(p.get("set_at", ""))
            if not dt or dt < cutoff:
                continue
            out.append(_event(
                dt,
                icon="fa-calendar-check",
                tone="info",
                text=f"Calendar updated: <strong>{_clip(p.get('label', ''), 50)}</strong>",
                section="calendar",
            ))
    except Exception as e:
        print(f"  [activity] calendar unreadable (non-fatal): {e}")

    # --- freshness: an admin dated a document ---------------------------
    try:
        from rag.freshness import list_docs
        for d in (list_docs() or []):
            dt = _parse(d.get("updated_at", ""))
            if not dt or dt < cutoff:
                continue
            out.append(_event(
                dt,
                icon="fa-clock-rotate-left",
                tone="info",
                text=f"<strong>{_clip(d.get('filename', ''), 45)}</strong> dated "
                     f"{d.get('effective_date') or 'unknown'}",
                section="freshness",
            ))
    except Exception as e:
        print(f"  [activity] freshness unreadable (non-fatal): {e}")

    out.sort(key=lambda e: e["_sort"], reverse=True)
    return out


def feed(limit: int = DEFAULT_LIMIT, *, extra: Optional[List[dict]] = None,
         window_days: int = DEFAULT_WINDOW_DAYS) -> List[dict]:
    """
    The rows to render, newest first.

    `extra` accepts pre-shaped rows from a caller that can reach data this module
    cannot — uploads and new accounts live in DynamoDB and Cognito, which
    `sc_assistant/admin.py` owns. They are merged into the same sort rather than
    appended, or a document uploaded a minute ago would sit below a vote from
    last week.
    """
    rows = events(window_days=window_days)

    for row in (extra or []):
        dt = _parse(row.get("at", ""))
        if not dt:
            continue
        merged = dict(row)
        merged["_sort"] = dt
        merged.setdefault("tone", "info")
        merged.setdefault("section", "")
        merged["time"] = humanize(dt.isoformat())
        rows.append(merged)

    rows.sort(key=lambda e: e["_sort"], reverse=True)

    # `_sort` is a datetime and would not survive jsonify. Dropped here rather
    # than at the call site so no caller can forget.

    trimmed = rows[: max(1, int(limit or DEFAULT_LIMIT))]
    for row in trimmed:
        row.pop("_sort", None)
    return trimmed


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


def attention() -> List[dict]:
    """
    What is waiting for a human, worst first.

    This is the answer to the question an admin actually opens the dashboard
    with. It replaces three "Total X" cards that could not be acted on: knowing
    there are 412 conversations tells nobody what to do next, whereas "2 students
    are waiting for a reply" does.

    Only cheap, already-stored counts appear here. Unresolved *conflicts* are
    deliberately absent: detecting those means re-parsing every source PDF (see
    `admin_conflicts._scan_sources`), which is far too expensive for a screen that
    loads on every visit and refreshes on a timer. The Conflicts section owns that
    scan, and the panel links to it instead of guessing.

    Rows are returned even when the count is zero, and the caller decides whether
    to render them; an empty panel is a stronger statement than a missing one.
    """
    rows: List[dict] = []

    def add(*, count: int, singular: str, plural: str, section: str,
            icon: str, tone: str, hint: str = ""):
        rows.append({
            "count": int(count or 0),
            "label": singular if count == 1 else plural,
            "section": section,
            "icon": icon,
            "tone": tone,
            "hint": hint,
        })

    # Students waiting on a human. First because a person is actually waiting.
    try:
        esc = escalation_stats()
        add(count=esc.get("pending", 0),
            singular="student is waiting for a reply",
            plural="students are waiting for a reply",
            section="escalations", icon="fa-hand", tone="bad",
            hint="Someone asked to be answered by an office.")
    except Exception as e:
        print(f"  [activity] escalation stats unreadable (non-fatal): {e}")

    # Questions the documents cannot answer — the upload roadmap.
    try:
        gaps = gap_stats()
        add(count=gaps.get("open_topics", 0),
            singular="topic the documents cannot answer",
            plural="topics the documents cannot answer",
            section="gaps", icon="fa-circle-question", tone="warn",
            hint="Each one is a document worth uploading.")
    except Exception as e:
        print(f"  [activity] gap stats unreadable (non-fatal): {e}")

    # Answers students said did not help.
    try:
        fb = feedback_stats()
        add(count=fb.get("topics_with_downvotes", 0),
            singular="topic students marked unhelpful",
            plural="topics students marked unhelpful",
            section="feedback", icon="fa-thumbs-down", tone="warn",
            hint="The answer was found but did not help.")
    except Exception as e:
        print(f"  [activity] feedback stats unreadable (non-fatal): {e}")

    return rows


def health() -> dict:
    """
    Two honest numbers about how the assistant is doing, for the strip under the
    triage panel.

    `satisfaction` is None — not 0.0 — when nobody has voted. A dashboard that
    reports 0% satisfaction because it has no data is lying in the most damaging
    direction possible, and `_rate` in analytics.py exists for the same reason.
    """
    out = {"votes": 0, "satisfaction": None, "answered": 0, "unanswered": 0}
    try:
        fb = feedback_stats()
        out["votes"] = fb.get("total_votes", 0)
        out["satisfaction"] = fb.get("satisfaction")
        out["answered"] = fb.get("total_votes", 0)
    except Exception as e:
        print(f"  [activity] health/feedback unreadable (non-fatal): {e}")
    try:
        out["unanswered"] = gap_stats().get("total_questions", 0)
    except Exception as e:
        print(f"  [activity] health/gaps unreadable (non-fatal): {e}")
    return out
