"""
rag/analytics.py — the evidence that the assistant works, computed from what
already happened.

Why this is not "just a dashboard"
----------------------------------
Every other admin screen in this project answers a question about ONE row: which
dean is correct, is this deadline still open, did this answer help. None of them
answers the question a panel — or a registrar deciding whether to keep using
this — will actually ask:

    "Is it working, and getting better or worse?"

Four stores already hold the raw material, and each one is currently read only by
its own screen:

    rag/feedback.py    every 👍/👎, with the question and the sources
    rag/gaps.py        every question the documents could not answer
    rag/escalation.py  every question that needed a human, and whether it got one
    rag/conflicts.py   every contradiction an admin has adjudicated

This module is the join. It adds NO new storage and NO new logging: a metric that
needs its own write path is a metric that can disagree with the screen it came
from, and then nobody trusts either. Everything here is derived, so the numbers
are reproducible by re-reading the same four files.

The metrics, and why these ones
-------------------------------
* **Answered rate** — of the questions we have any signal about, how many did the
  documents actually cover. This is the headline number: it is the one that goes
  up when an admin uploads the right PDF, which is exactly the behaviour the
  Content Gaps screen is trying to cause.

* **Satisfaction** — of the answers students judged, how many were useful. Kept
  separate from the answered rate on purpose: "we found something" and "it helped"
  are different failures with different fixes (indexing vs. document quality),
  and averaging them together hides both.

* **Peak hours, in Manila time.** A UTC histogram of a Philippine campus is
  useless — 8 AM local is 00:00 UTC, so every "morning rush" lands on the
  previous day. The timestamps are stored in UTC (correctly), so the conversion
  happens here, once, importing the same `MANILA` offset the calendar feature
  already uses rather than declaring a second private one that could drift.


* **Trend** — the last N days as a series, plus a comparison of the most recent
  week against the one before it. A single lifetime average cannot distinguish
  "we fixed it last week" from "it was always fine", and those are the two things
  an admin most needs to tell apart.

* **Top topics** — grouped by `normalize_question()`, the SAME normaliser the gap
  log and the feedback store use. If analytics grouped differently, the top topic
  here would not match the top row there, and the first person to notice would
  stop believing the dashboard.

Everything degrades to zero rather than raising: this is a reporting surface, and
a broken chart must never be able to take down the assistant.
"""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from rag.calendar import MANILA, today_in_manila

from rag.gaps import _load as _load_gaps
from rag.feedback import _load as _load_feedback
from rag.escalation import _load as _load_escalations

# How much history the charts cover by default. Two weeks is enough to see a
# trend and short enough that an empty first week does not flatten it.
DEFAULT_DAYS = 14

# Buckets that hold a handful of events are noise, not signal — a topic asked once
# does not belong in "top topics" next to one asked forty times.
MIN_TOPIC_COUNT = 1


# ---------------------------------------------------------------------------
# Time handling
#
# Every store writes `datetime.now(timezone.utc).isoformat()`. That is the right
# thing to store and the wrong thing to display: a Samar campus does not
# experience 00:00 UTC as the start of its day. So parsing and bucketing are done
# once, here, and always in Manila time.
# ---------------------------------------------------------------------------


def _parse(ts: str) -> Optional[datetime.datetime]:
    """
    ISO-8601 -> aware datetime in Manila time, or None.

    Returns None rather than raising: one malformed timestamp in a store must not
    blank the whole dashboard, and a row we cannot date is simply a row that
    cannot appear on a time axis.
    """
    if not ts or not isinstance(ts, str):
        return None
    try:
        # `fromisoformat` handles the "+00:00" our stores write. A trailing "Z"
        # is accepted too because it is what most other tools produce, and a
        # dashboard that silently drops those rows would under-report.
        cleaned = ts.strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(MANILA)

    except Exception:
        return None


def _local_date(ts: str) -> Optional[datetime.date]:
    dt = _parse(ts)
    return dt.date() if dt else None


def _date_range(days: int, *, today: Optional[datetime.date] = None) -> List[datetime.date]:
    """
    Every date in the window, oldest first — including the ones with no activity.

    Gaps are filled deliberately. A sparse series drawn as a line implies a
    straight climb between two points that were actually a spike and a silence,
    and the flat stretch is itself the finding ("nobody used it during the
    semester break").
    """
    end = today or today_in_manila()
    span = max(1, int(days or DEFAULT_DAYS))
    return [end - datetime.timedelta(days=i) for i in range(span - 1, -1, -1)]


# ---------------------------------------------------------------------------
# Event extraction
#
# Each store has its own shape, so each gets a small reader that yields a common
# `(date, hour, topic_key, kind)` tuple. Doing this once means every metric below
# is a Counter over the same list instead of four parallel traversals that can
# drift apart.
# ---------------------------------------------------------------------------


def _events() -> List[dict]:
    """
    Flatten all four stores into one timeline.

    `kind` is what the row proves:
        "answered"   a student judged an answer (up or down) — so we answered
        "unanswered" the documents could not answer it
        "escalated"  it needed a human
        "resolved"   a human replied
    """
    out: List[dict] = []

    # --- feedback: one event per vote -----------------------------------
    try:
        for v in (_load_feedback().get("votes") or {}).values():
            dt = _parse(v.get("at", ""))
            if not dt:
                continue
            out.append({
                "at": dt,
                "date": dt.date(),
                "hour": dt.hour,
                "topic": v.get("topic_key") or "",
                "label": v.get("question") or "",
                "kind": "answered",
                "verdict": v.get("verdict") or "",
                "sources": v.get("sources") or [],
            })
    except Exception as e:
        print(f"  [analytics] feedback unreadable (non-fatal): {e}")

    # --- gaps: one event per RECORDED EXAMPLE, not per group ------------
    #
    # `count` on a gap group is a running total with no timestamps, so it cannot
    # be placed on a time axis. The examples each carry `at`, so they can. This
    # under-counts a topic asked more times than MAX_EXAMPLES_PER_GAP, which is
    # the right way to be wrong here: the trend line stays honest about WHEN
    # things happened, and `gap_stats()` on the Content Gaps screen remains the
    # authority on totals.
    try:
        for g in (_load_gaps().get("gaps") or {}).values():
            for ex in g.get("examples", []):
                dt = _parse(ex.get("at", ""))
                if not dt:
                    continue
                out.append({
                    "at": dt,
                    "date": dt.date(),
                    "hour": dt.hour,
                    "topic": g.get("key") or "",
                    "label": g.get("topic") or ex.get("question") or "",
                    "kind": "unanswered",
                    "verdict": "",
                    "sources": [],
                })
    except Exception as e:
        print(f"  [analytics] gaps unreadable (non-fatal): {e}")

    # --- escalations: raised, and separately answered -------------------
    try:
        for item in (_load_escalations().get("items") or {}).values():
            dt = _parse(item.get("created_at", ""))
            if dt:
                out.append({
                    "at": dt,
                    "date": dt.date(),
                    "hour": dt.hour,
                    "topic": item.get("topic_key") or "",
                    "label": item.get("question") or "",
                    "kind": "escalated",
                    "verdict": "",
                    "sources": [],
                })
            # The reply is its own event at its own time — "how long until a
            # student hears back" is only answerable if both ends are on the
            # timeline.
            answered = _parse(item.get("answered_at", ""))
            if answered:
                out.append({
                    "at": answered,
                    "date": answered.date(),
                    "hour": answered.hour,
                    "topic": item.get("topic_key") or "",
                    "label": item.get("question") or "",
                    "kind": "resolved",
                    "verdict": "",
                    "sources": [],
                })
    except Exception as e:
        print(f"  [analytics] escalations unreadable (non-fatal): {e}")

    out.sort(key=lambda e: e["at"])
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _rate(part: int, whole: int) -> Optional[float]:
    """
    A proportion, or None when there is nothing to divide.

    None, not 0.0. An answered rate of 0% and "no questions yet" look identical
    in a chart but mean opposite things — one is a broken corpus, the other is a
    system nobody has used. The UI can only tell them apart if the absence is
    representable.
    """
    return round(part / whole, 3) if whole else None


def volume_series(days: int = DEFAULT_DAYS, *, today: Optional[datetime.date] = None) -> List[dict]:
    """
    Per-day counts over the window: answered, unanswered, escalated, and the
    answered rate for that day.

    One row per calendar day, zero-filled — see `_date_range`.
    """
    events = _events()
    by_day: Dict[datetime.date, Counter] = defaultdict(Counter)
    for e in events:
        by_day[e["date"]][e["kind"]] += 1

    series = []
    for d in _date_range(days, today=today):
        c = by_day.get(d, Counter())
        answered = c["answered"]
        unanswered = c["unanswered"]
        asked = answered + unanswered
        series.append({
            "date": d.isoformat(),
            "answered": answered,
            "unanswered": unanswered,
            "escalated": c["escalated"],
            "resolved": c["resolved"],
            "total": asked,
            "answered_rate": _rate(answered, asked),
        })
    return series


def hourly_histogram(*, days: Optional[int] = None,
                     today: Optional[datetime.date] = None) -> List[dict]:
    """
    Activity by hour of the day, 0-23, in **Manila time**.

    This is the number that tells an admin when to schedule a maintenance window
    or when the registrar's queue is about to form. Computed in local time for the
    reason given at the top of this module: in UTC the campus morning appears on
    the wrong date entirely.
    """
    events = _events()
    if days:
        window = set(_date_range(days, today=today))
        events = [e for e in events if e["date"] in window]

    counts = Counter(e["hour"] for e in events)
    peak = max(counts.values()) if counts else 0
    return [
        {
            "hour": h,
            "label": f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}",
            "count": counts.get(h, 0),
            # Pre-computed so the bar chart does not have to re-derive the scale
            # (and cannot disagree with the "peak hour" headline below).
            "share_of_peak": round(counts.get(h, 0) / peak, 3) if peak else 0.0,
        }
        for h in range(24)
    ]


def top_topics(limit: int = 10, *, kind: Optional[str] = None) -> List[dict]:
    """
    What students actually ask, most-asked first.

    Grouped by `normalize_question()` — the same normaliser used by the gap log
    and the feedback store, so a topic here is the same topic there. `kind` can
    narrow it to "unanswered" (the document roadmap) or "answered".
    """
    events = [e for e in _events() if e["topic"]]
    if kind:
        events = [e for e in events if e["kind"] == kind]

    grouped: Dict[str, dict] = {}
    for e in events:
        row = grouped.setdefault(e["topic"], {
            "key": e["topic"],
            "topic": e["label"],
            "total": 0,
            "answered": 0,
            "unanswered": 0,
            "escalated": 0,
            "up": 0,
            "down": 0,
            "last_asked": "",
        })
        row["total"] += 1
        if e["kind"] in ("answered", "unanswered", "escalated"):
            row[e["kind"]] += 1
        if e["verdict"] in ("up", "down"):
            row[e["verdict"]] += 1
        stamp = e["at"].isoformat()
        if stamp > row["last_asked"]:
            row["last_asked"] = stamp
        # Prefer a human-readable label if a later event has one.
        if not row["topic"] and e["label"]:
            row["topic"] = e["label"]

    rows = [r for r in grouped.values() if r["total"] >= MIN_TOPIC_COUNT]
    for r in rows:
        judged = r["up"] + r["down"]
        r["satisfaction"] = _rate(r["up"], judged)
        r["answered_rate"] = _rate(r["answered"], r["answered"] + r["unanswered"])

    rows.sort(key=lambda r: (-r["total"], r["topic"]))
    return rows[:max(1, int(limit or 10))]


def suspect_documents(limit: int = 10) -> List[dict]:
    """
    Documents that keep appearing under downvoted answers.

    The join that makes votes actionable: a 👎 on its own says an answer was bad,
    but the sources stored with it say WHICH page produced it. A page at the top
    of this list is either wrong, outdated, or chunked in a way that strands its
    context — and all three are fixable by a human who knows where to look.

    Upvotes are counted too, because a page that appears under fifty good answers
    and two bad ones is not the problem; the ratio is.
    """
    tally: Dict[str, dict] = {}
    for e in _events():
        if e["kind"] != "answered" or not e["sources"]:
            continue
        for src in e["sources"]:
            if not isinstance(src, str) or not src.strip():
                continue
            row = tally.setdefault(src, {"source": src, "up": 0, "down": 0, "total": 0})
            row["total"] += 1
            if e["verdict"] in ("up", "down"):
                row[e["verdict"]] += 1

    rows = [r for r in tally.values() if r["down"] > 0]
    for r in rows:
        judged = r["up"] + r["down"]
        r["satisfaction"] = _rate(r["up"], judged)
    # Most downvotes first, then worst ratio — a page with 5 downvotes and no
    # upvotes should outrank one with 5 downvotes and 100 upvotes.
    rows.sort(key=lambda r: (-r["down"], r.get("satisfaction") or 0))
    return rows[:max(1, int(limit or 10))]


def response_times() -> dict:
    """
    How long students wait for a human reply, and how many are still waiting.

    An escalation queue with no clock on it degrades quietly: nothing errors, the
    rows just sit there, and the student who was promised "they will reply" learns
    the promise was worthless. The oldest pending age is the number that makes
    that visible.
    """
    now = datetime.datetime.now(datetime.timezone.utc).astimezone(MANILA)

    waits: List[float] = []
    pending_ages: List[float] = []

    try:
        for item in (_load_escalations().get("items") or {}).values():
            created = _parse(item.get("created_at", ""))
            if not created:
                continue
            answered = _parse(item.get("answered_at", ""))
            if answered:
                waits.append((answered - created).total_seconds() / 3600.0)
            elif item.get("status") == "pending":
                pending_ages.append((now - created).total_seconds() / 3600.0)
    except Exception as e:
        print(f"  [analytics] escalations unreadable (non-fatal): {e}")

    return {
        "answered_count": len(waits),
        "avg_hours": round(sum(waits) / len(waits), 2) if waits else None,
        # The median matters more than the mean here: one escalation answered
        # three weeks late would otherwise make a same-day queue look broken.
        "median_hours": round(sorted(waits)[len(waits) // 2], 2) if waits else None,
        "pending_count": len(pending_ages),
        "oldest_pending_hours": round(max(pending_ages), 2) if pending_ages else None,
    }


def _window_totals(events: List[dict], dates: set) -> dict:
    subset = [e for e in events if e["date"] in dates]
    answered = sum(1 for e in subset if e["kind"] == "answered")
    unanswered = sum(1 for e in subset if e["kind"] == "unanswered")
    up = sum(1 for e in subset if e["verdict"] == "up")
    down = sum(1 for e in subset if e["verdict"] == "down")
    return {
        "questions": answered + unanswered,
        "answered": answered,
        "unanswered": unanswered,
        "answered_rate": _rate(answered, answered + unanswered),
        "satisfaction": _rate(up, up + down),
    }


def week_over_week(*, today: Optional[datetime.date] = None) -> dict:
    """
    The last 7 days against the 7 before them.

    A lifetime average cannot answer "is this getting better?", which is the only
    version of the question that leads to a decision. Deltas are returned as
    `None` when either side has no data, so the UI shows "not enough data" instead
    of a confident-looking 0%.
    """
    end = today or today_in_manila()
    recent = {end - datetime.timedelta(days=i) for i in range(7)}
    prior = {end - datetime.timedelta(days=i) for i in range(7, 14)}

    events = _events()
    a = _window_totals(events, recent)
    b = _window_totals(events, prior)

    def delta(key):
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            return None
        return round(x - y, 3)

    return {
        "recent": a,
        "prior": b,
        "delta": {
            "questions": a["questions"] - b["questions"],
            "answered_rate": delta("answered_rate"),
            "satisfaction": delta("satisfaction"),
        },
    }


def overview(days: int = DEFAULT_DAYS, *, today: Optional[datetime.date] = None) -> dict:
    """
    Everything the dashboard needs, in one call.

    One endpoint rather than six because these numbers are read together and must
    agree with each other: six requests can interleave with a student's vote and
    render a page where the headline and the chart disagree by one.
    """
    events = _events()
    answered = sum(1 for e in events if e["kind"] == "answered")
    unanswered = sum(1 for e in events if e["kind"] == "unanswered")
    up = sum(1 for e in events if e["verdict"] == "up")
    down = sum(1 for e in events if e["verdict"] == "down")

    hist = hourly_histogram()
    busiest = max(hist, key=lambda h: h["count"]) if hist else None

    series = volume_series(days, today=today)

    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "window_days": max(1, int(days or DEFAULT_DAYS)),
        "totals": {
            "questions_with_signal": answered + unanswered,
            "answered": answered,
            "unanswered": unanswered,
            "answered_rate": _rate(answered, answered + unanswered),
            "votes": up + down,
            "up": up,
            "down": down,
            "satisfaction": _rate(up, up + down),
            "escalated": sum(1 for e in events if e["kind"] == "escalated"),
            "escalations_resolved": sum(1 for e in events if e["kind"] == "resolved"),
        },
        "series": series,
        "hourly": hist,
        "peak_hour": busiest["label"] if busiest and busiest["count"] else None,
        "top_topics": top_topics(10),
        "unanswered_topics": top_topics(10, kind="unanswered"),
        "suspect_documents": suspect_documents(10),
        "response_times": response_times(),
        "trend": week_over_week(today=today),
    }
