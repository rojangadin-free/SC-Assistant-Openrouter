"""
rag/announcements.py — the answer to "classes are suspended tomorrow".

The gap this fills
------------------
Every other source of truth in this project is a document. That works for facts
with a long shelf life — programs, fees, requirements — and fails completely for
the ones that matter most on the day they matter:

    "classes are suspended tomorrow due to the typhoon"
    "enrollment has been extended to June 20"
    "the registrar's office is closed this afternoon"

No PDF will ever cover these. By the time a handbook is edited, re-uploaded and
re-indexed, the announcement is history. So a student asking "may klase ba
bukas?" during a storm gets the most confident possible wrong answer: the regular
class schedule, correctly cited, from a document that has no idea a typhoon is
happening.

`rag/calendar.py` solved the *scheduled* version of this problem. This module
solves the *unscheduled* one, which is the more dangerous half: a wrong deadline
costs a student a late fee, but "yes, come to campus" during a suspension sends
them out in a storm.

Two jobs, one record
--------------------
An announcement is both a **notice** (pinned in the chat UI, so a student who
never asks still sees it) and **context** (injected into the prompt at the same
authority level as an admin-verified fact, so a student who asks gets the new
answer rather than the document's answer).

Doing only the first is a noticeboard nobody reads. Doing only the second means
the information exists but only reaches people who thought to ask. Both come from
one admin action here.

Design notes
------------
* **Highest authority, deliberately.** `authority_block()` outranks retrieved
  documents because an admin typed it; the same argument applies here, only more
  strongly — an announcement is not merely newer than the PDF, it is *about* the
  PDF being temporarily wrong.

* **Everything expires.** An announcement with no end date is a permanent one, and
  a permanent "classes suspended tomorrow" is a lie by the following week. The
  admin picks how long it lives; `active_announcements()` filters on every read,
  so a stale notice stops reaching students without anyone remembering to delete
  it. Expiry is a date, not a delete: the record stays for the audit trail.

* **Unpinned still counts.** Pinning controls whether the banner appears. It does
  not control whether the prompt sees it. A low-key "the registrar closes at 3pm
  today" should not need a banner to make the assistant answer correctly.

* **Priority is about *display*, not truth.** All active announcements reach the
  model. `urgent` only decides what the student sees first and how loud it looks,
  because in a genuine emergency the ordering of a list is the feature.

* **No keyword gating.** Unlike the calendar, which only loads for timing
  questions, an active announcement is injected into *every* request. A suspension
  is relevant to "is the library open", "should i come tomorrow" and "what time is
  my class" — enumerating those phrasings in advance is exactly the guess this
  module cannot afford to get wrong. The cost is a few dozen tokens; the failure
  mode of the alternative is a student walking into a typhoon.
"""

from __future__ import annotations

import datetime
import os
import re
import uuid
from typing import Dict, List, Optional

from rag.calendar import MANILA, today_in_manila
from rag.store import JsonBlobStore

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

STORE_PATH = os.getenv("ANNOUNCEMENTS_FILE", "announcements.json")

_store = JsonBlobStore(
    name="announcements",
    path=STORE_PATH,
    default=lambda: {"announcements": []},
)

# How loud, in the UI only. Ordered most- to least-urgent; the list order *is* the
# sort order, so adding a level in the middle is a one-line change.
PRIORITIES = ("urgent", "important", "info")

# Audiences. An announcement about faculty payroll reaching every student is
# noise, and noise is how a banner becomes something people learn to ignore.
AUDIENCES = {
    "all": "Everyone",
    "students": "Students",
    "faculty": "Faculty & Staff",
}

MAX_ANNOUNCEMENTS = 200  # bounded so the store stays one small round-trip
MAX_TITLE = 200
MAX_BODY = 2000


def _now_iso() -> str:
    return datetime.datetime.now(MANILA).isoformat()


def _parse_date(raw) -> Optional[datetime.date]:
    if not raw:
        return None
    if isinstance(raw, datetime.date):
        return raw
    try:
        return datetime.date.fromisoformat(str(raw).strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _sort_key(a: dict) -> tuple:
    """Urgent first, then newest. Priority beats recency on purpose: during a
    storm the suspension notice must not be pushed down by a routine post made
    ten minutes later."""
    try:
        rank = PRIORITIES.index(a.get("priority", "info"))
    except ValueError:
        rank = len(PRIORITIES)
    # Descending timestamp within a priority band.
    return (rank, _neg_str(a.get("created_at", "")))


def _neg_str(s: str) -> tuple:
    """Sort strings descending inside an ascending tuple sort."""
    # ISO timestamps compare lexicographically, so inverting each codepoint gives
    # a descending order without needing `reverse=` (which would flip priority
    # too). Cheap, and keeps the whole comparison in one key function.
    return tuple(-ord(c) for c in s)


def list_announcements(*, include_expired: bool = True) -> List[dict]:
    data = _store.load()
    items = [a for a in data.get("announcements", []) if isinstance(a, dict)]
    if not include_expired:
        items = [a for a in items if not is_expired(a)]
    return sorted(items, key=_sort_key)


def is_expired(a: dict, today: Optional[datetime.date] = None) -> bool:
    """
    Expired means "the admin's own end date has passed".

    Note the boundary: an announcement ending today is still active *all* of
    today. "Classes suspended until Friday" must survive Friday, or the notice
    disappears on the very morning it is about.
    """
    end = _parse_date(a.get("expires_on"))
    if not end:
        return False
    return (today or today_in_manila()) > end


def active_announcements(
    *,
    audience: str = "all",
    today: Optional[datetime.date] = None,
) -> List[dict]:
    """
    What a given reader should see right now.

    `audience="all"` is the *reader* asking for everything (the admin screen and
    the prompt). A student passes "students", which returns announcements aimed
    at students plus the ones aimed at everyone — never faculty-only ones.
    """
    today = today or today_in_manila()
    out = []
    for a in list_announcements():
        if is_expired(a, today):
            continue
        if not a.get("active", True):
            continue
        target = a.get("audience", "all")
        if audience != "all" and target not in ("all", audience):
            continue
        # A start date lets an admin write the notice the night before and have it
        # appear on the day, instead of remembering to post it at 6am.
        starts = _parse_date(a.get("starts_on"))
        if starts and today < starts:
            continue
        out.append(a)
    return out


def pinned_announcements(**kwargs) -> List[dict]:
    """Active AND pinned — the subset the chat UI shows unprompted."""
    return [a for a in active_announcements(**kwargs) if a.get("pinned", True)]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def save_announcement(
    title: str,
    body: str,
    *,
    key: str = "",
    priority: str = "info",
    audience: str = "all",
    starts_on: str = "",
    expires_on: str = "",
    pinned: bool = True,
    posted_by: str = "",
) -> Optional[dict]:
    """
    Create, or correct in place when `key` names an existing row.

    Returns None on invalid input — an unparseable date or an end before the
    start — rather than saving something the admin did not mean. Silently
    coercing a bad date here would produce an announcement that either never
    appears or never goes away.
    """
    title = (title or "").strip()[:MAX_TITLE]
    body = (body or "").strip()[:MAX_BODY]
    if not title:
        return None

    if priority not in PRIORITIES:
        priority = "info"
    if audience not in AUDIENCES:
        audience = "all"

    start = _parse_date(starts_on) if starts_on else None
    end = _parse_date(expires_on) if expires_on else None
    if starts_on and not start:
        return None
    if expires_on and not end:
        return None
    if start and end and end < start:
        return None

    record = {
        "key": key or uuid.uuid4().hex[:12],
        "title": title,
        "body": body,
        "priority": priority,
        "audience": audience,
        "starts_on": start.isoformat() if start else "",
        "expires_on": end.isoformat() if end else "",
        "pinned": bool(pinned),
        "active": True,
        "posted_by": posted_by,
    }

    saved: Dict[str, dict] = {}

    def _apply(data: dict):
        items = data.setdefault("announcements", [])
        for i, existing in enumerate(items):
            if existing.get("key") == record["key"]:
                # Preserve the original post time on an edit: "posted 3 days ago"
                # must not reset because a typo was fixed.
                record["created_at"] = existing.get("created_at", _now_iso())
                record["updated_at"] = _now_iso()
                items[i] = record
                saved["r"] = record
                return
        record["created_at"] = _now_iso()
        items.append(record)
        # Oldest-first eviction, and only among expired rows, so a burst of
        # routine posts can never push out a still-active suspension notice.
        if len(items) > MAX_ANNOUNCEMENTS:
            items.sort(key=lambda a: (not is_expired(a), a.get("created_at", "")))
            del items[: len(items) - MAX_ANNOUNCEMENTS]
        saved["r"] = record

    _store.mutate(_apply)
    return saved.get("r")


def delete_announcement(key: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False

    removed = {"ok": False}

    def _apply(data: dict):
        items = data.get("announcements", [])
        keep = [a for a in items if a.get("key") != key]
        if len(keep) == len(items):
            return False  # nothing to do; skip the write
        data["announcements"] = keep
        removed["ok"] = True

    _store.mutate(_apply)
    return removed["ok"]


def set_active(key: str, active: bool) -> Optional[dict]:
    """
    Take an announcement down (or put it back) without deleting it.

    Deleting loses the record of what students were told and when, which is
    exactly what someone will ask about after a suspension. This is the reversible
    version.
    """
    key = (key or "").strip()
    found = {}

    def _apply(data: dict):
        for a in data.get("announcements", []):
            if a.get("key") == key:
                a["active"] = bool(active)
                a["updated_at"] = _now_iso()
                found["r"] = a
                return
        return False

    _store.mutate(_apply)
    return found.get("r")


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------

def _human(iso: str) -> str:
    d = _parse_date(iso)
    if not d:
        return ""
    # No %-d / %#d: the two platforms disagree about which strips the zero, and
    # this string goes into a prompt read by a model that will happily repeat
    # "June 05".
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def announcement_block(
    *,
    audience: str = "students",
    today: Optional[datetime.date] = None,
) -> str:
    """
    The `<announcements>` section of the system prompt.

    Returns "" when there is nothing active — an empty tag would spend tokens
    telling the model that nothing is happening, and invites it to mention that.

    The wording is imperative because this block exists to make the model
    *contradict* its own retrieved context. A polite "you may also consider" does
    not beat a handbook paragraph that states class hours as fact.
    """
    items = active_announcements(audience=audience, today=today)
    if not items:
        return ""

    today = today or today_in_manila()
    lines = [
        "<announcements>",
        "OFFICIAL ANNOUNCEMENTS posted by Samar College administration. These are",
        "CURRENT and OVERRIDE anything in the documents above that contradicts them.",
        "If one applies to the question, lead with it, state it as fact, and do NOT",
        "hedge or present the document's version as an alternative.",
        "",
    ]

    for a in items:
        marker = {
            "urgent": "[URGENT]",
            "important": "[IMPORTANT]",
        }.get(a.get("priority", "info"), "[NOTICE]")

        line = f"- {marker} {a['title']}"

        window = []
        if a.get("starts_on"):
            window.append(f"effective {_human(a['starts_on'])}")
        if a.get("expires_on"):
            end = _parse_date(a["expires_on"])
            days = (end - today).days if end else None
            # Spelled out, for the same reason as the calendar block: the model
            # must never be the one subtracting dates.
            window.append(
                "in effect today only" if days == 0
                else f"in effect until {_human(a['expires_on'])} ({days} more day(s))"
            )
        if window:
            line += f" ({'; '.join(window)})"

        lines.append(line)
        if a.get("body"):
            # One line, so the block stays scannable for the model. Newlines in a
            # bullet list have a habit of being read as separate items.
            flat = re.sub(r"\s+", " ", a["body"]).strip()
            lines.append(f"  {flat}")


    lines.append("</announcements>")
    return "\n".join(lines)


def stats(*, today: Optional[datetime.date] = None) -> dict:
    all_items = list_announcements()
    live = active_announcements(audience="all", today=today)
    return {
        "total": len(all_items),
        "active": len(live),
        "pinned": len([a for a in live if a.get("pinned", True)]),
        "urgent": len([a for a in live if a.get("priority") == "urgent"]),
        "expired": len([a for a in all_items if is_expired(a, today)]),
    }
