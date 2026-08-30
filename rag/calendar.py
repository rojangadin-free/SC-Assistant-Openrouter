"""
rag/calendar.py — make the assistant aware of WHEN it is being asked.

The bug this solves
-------------------
`src/prompt.py` already interpolates today's date, and that is not enough. The
model knows the date; it does not know what the date *means* to a student:

    student (June 3) : "when is enrollment?"
    assistant        : "Enrollment for the First Semester is from June 1-15."

Correct, cited, and useless — enrollment closes in twelve days and nothing in the
answer says so. Worse is the symmetric failure, where a student asking in October
gets the June dates recited in the present tense as if they were upcoming.

A date is a fact. A deadline is a fact *plus* the distance to it, and that second
part is arithmetic the LLM should never be asked to do: "is 2026-06-15 more than
twelve days after 2026-06-03" is exactly the kind of question models answer
confidently and wrongly.

What this module does
---------------------
1. `parse_periods()` — pull `(label, start, end)` periods out of raw document
   text using shape-based date patterns. No Samar-College vocabulary.
2. `current_state()`  — given a date, classify every period as past / active /
   upcoming and compute the day distance.
3. `calendar_block()` — render only what is relevant into the prompt, in plain
   language the model cannot get wrong: "Enrollment is OPEN and closes in 12
   days (June 15, 2026)."

The admin side (`ADMIN_PERIODS`) exists because dates are the fastest-rotating
facts in the corpus and re-uploading a PDF to change one deadline is too slow.
An admin-entered period always beats a parsed one — same precedence rule as
`rag.conflicts`, for the same reason.

Design notes
------------
* **Only relevant periods reach the prompt.** Dumping the full academic calendar
  into every request would cost tokens on questions that never mention dates, and
  would encourage the model to volunteer deadlines nobody asked about.

* **"Today" is a parameter, not a call to `now()`.** Every function takes an
  explicit `today`, which is what makes the behaviour testable: a suite that has
  to wait for June to check the June branch is not a suite.

* **Timezone is Asia/Manila, fixed.** The server may run anywhere; the deadline is
  local to the campus. Computing "days until" in UTC puts the boundary in the
  wrong place for eight hours a day, which is precisely when a student checking
  the night before a deadline needs it right.
"""

from __future__ import annotations

import datetime
import os
import re
import threading
from typing import Dict, List, Optional, Tuple

from rag.store import JsonBlobStore

# ---------------------------------------------------------------------------
# Campus time
# ---------------------------------------------------------------------------
# +08:00, hardcoded rather than read from the OS. A deployment in another region
# would otherwise silently shift every "closes in N days" by a day, and the whole
# point of this module is to be trusted about exactly that number.
MANILA = datetime.timezone(datetime.timedelta(hours=8))


def today_in_manila() -> datetime.date:
    return datetime.datetime.now(MANILA).date()


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

# "June 1-15, 2026" / "June 1 - 15, 2026" — one month, a day range.
_RANGE_SAME_MONTH = re.compile(
    rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})\s*[-–—]\s*(\d{{1,2}})(?:,?\s*(\d{{4}}))?",
    re.I,
)

# "June 1 – July 15, 2026" — two months.
_RANGE_CROSS_MONTH = re.compile(
    rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:,?\s*(\d{{4}}))?\s*[-–—]\s*"
    rf"({_MONTH_RE})\.?\s+(\d{{1,2}})(?:,?\s*(\d{{4}}))?",
    re.I,
)

# "June 15, 2026" — a single day.
_SINGLE = re.compile(
    rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})\b",
    re.I,
)

# The label is whatever precedes the date on the same line. Deliberately dumb:
# any wording works, because the alternative is a list of expected event names
# that silently ignores the one the college actually used.
_LABEL_TRAILING_PUNCT = re.compile(r"[\s:–—\-•\.]+$")


def _mk_date(year: int, month: int, day: int) -> Optional[datetime.date]:
    """Build a date, or None if the numbers are not a real day (e.g. Feb 31)."""
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _clean_label(raw: str) -> str:
    """
    Turn the text before a date into a short event label.

    Only the tail of the line is kept: a paragraph mentioning a date is not an
    event, and taking the whole sentence would produce labels too long to show
    and too specific to match a student's question.
    """
    text = _LABEL_TRAILING_PUNCT.sub("", (raw or "").strip())
    # A label is a phrase, not a sentence — start after the last sentence break.
    for sep in (".", ";", "|", "\u2022"):
        if sep in text:
            text = text.rsplit(sep, 1)[-1]
    words = text.split()
    return " ".join(words[-8:])[:80].strip()


def parse_periods(text: str, *, default_year: Optional[int] = None) -> List[dict]:
    """
    Extract `{label, start, end}` periods from one page of raw text.

    `default_year` fills in a year the document omitted ("June 1-15" with the year
    only in a heading). It defaults to the current campus year — wrong for a
    historical document, but a period with no year is unusable otherwise, and
    `current_state()` labels anything long past as `past` rather than acting on it.

    The three patterns are tried widest-first and each match **consumes** the
    characters it used. Without that, "December 20, 2026 - January 5, 2027" is
    read three times over — once correctly as a range, then again as the single
    date "December 20" and as "January 5" with the first half of the line
    absorbed into its label. One event would arrive at the admin's review screen
    as three rows, two of them wrong, which is precisely the duplicate-data
    problem this project exists to remove.
    """
    if not text:
        return []

    year_hint = default_year or today_in_manila().year
    out: List[dict] = []
    seen: set = set()

    def add(label: str, start: datetime.date, end: datetime.date):
        key = (label.lower(), start, end)
        if key in seen:
            return
        seen.add(key)
        out.append({"label": label, "start": start.isoformat(), "end": end.isoformat()})

    for line in text.splitlines():
        # Spans already claimed by a wider pattern on this line. A narrower
        # pattern that overlaps one of them is a re-reading of the same dates,
        # not a second event.
        claimed: List[Tuple[int, int]] = []

        def overlaps(m) -> bool:
            return any(m.start() < c_end and m.end() > c_start for c_start, c_end in claimed)

        # Cross-month first: "June 1 – July 15" also matches the same-month
        # pattern on its leading half, and taking that would report a period
        # ending in the wrong month.
        for m in _RANGE_CROSS_MONTH.finditer(line):
            m1, d1, y1, m2, d2, y2 = m.groups()
            y2i = int(y2) if y2 else year_hint
            # A missing first year takes the second one: "Nov 20 – Jan 5, 2027"
            # means the range starts in 2026, so borrow and step back a year when
            # the months wrap.
            y1i = int(y1) if y1 else (y2i - 1 if _MONTHS[m1.lower()] > _MONTHS[m2.lower()] else y2i)
            start = _mk_date(y1i, _MONTHS[m1.lower()], int(d1))
            end = _mk_date(y2i, _MONTHS[m2.lower()], int(d2))
            if start and end and start <= end:
                add(_clean_label(line[: m.start()]), start, end)
                claimed.append((m.start(), m.end()))

        for m in _RANGE_SAME_MONTH.finditer(line):
            if overlaps(m):
                continue
            mon, d1, d2, y = m.groups()
            yi = int(y) if y else year_hint
            start = _mk_date(yi, _MONTHS[mon.lower()], int(d1))
            end = _mk_date(yi, _MONTHS[mon.lower()], int(d2))
            if start and end and start <= end:
                add(_clean_label(line[: m.start()]), start, end)
                claimed.append((m.start(), m.end()))

        for m in _SINGLE.finditer(line):
            if overlaps(m):
                continue
            mon, d, y = m.groups()
            day = _mk_date(int(y), _MONTHS[mon.lower()], int(d))
            if not day:
                continue
            # A single date is a one-day period. Modelling it as start == end
            # means `current_state()` needs no special case for deadlines.
            label = _clean_label(line[: m.start()])
            if label:
                add(label, day, day)
                claimed.append((m.start(), m.end()))


    return out


# ---------------------------------------------------------------------------
# Admin-entered periods (these outrank anything parsed)
# ---------------------------------------------------------------------------

PERIODS_FILE = os.getenv("CALENDAR_PERIODS_FILE", "calendar_periods.json")


def _empty_store() -> dict:
    return {"periods": {}, "updated_at": ""}


_store_obj: Optional[JsonBlobStore] = None
_store_obj_path: Optional[str] = None
_store_init_lock = threading.Lock()
_lock = threading.Lock()


def _store() -> JsonBlobStore:
    global _store_obj, _store_obj_path
    path = os.getenv("CALENDAR_PERIODS_FILE", PERIODS_FILE)
    with _store_init_lock:
        if _store_obj is None or _store_obj_path != path:
            _store_obj = JsonBlobStore("calendar", path, _empty_store)
            _store_obj_path = path
        return _store_obj


def _load() -> dict:
    data = _store().load()
    return data if "periods" in data else _empty_store()


def _save(store: dict) -> None:
    _store().save(store)


def set_period(
    label: str,
    start: str,
    end: str,
    *,
    note: Optional[str] = None,
    set_by: str = "",
) -> Optional[dict]:
    """
    Record an authoritative period. Returns the row, or None if the dates are
    unusable.

    Keyed by the lowercased label, so re-entering "Enrollment" corrects the
    existing row instead of creating a second one — two rows for the same event
    is the conflict this whole feature exists to prevent.

    Two things are preserved across a correction, and both were bugs first:

    * **The note.** `note` defaults to None, not "". Correcting only a date used
      to blank the note, because "not supplied" and "cleared" were the same
      value — so "Late fee after June 10" vanished from the prompt the moment
      someone pushed the end date back a week. Passing `note=""` still clears it
      deliberately.
    * **The original capitalisation.** Re-saving as "enrollment" used to rewrite
      the display label to lowercase, and that label is what the student reads in
      the answer.
    """
    label = " ".join((label or "").split())
    if not label:
        return None
    try:
        s = datetime.date.fromisoformat((start or "").strip())
        e = datetime.date.fromisoformat((end or "").strip()) if end else s
    except ValueError:
        return None
    if e < s:
        return None

    key = label.lower()
    with _lock:
        store = _load()
        existing = store.setdefault("periods", {}).get(key, {})
        # A correction keeps the capitalisation the admin originally typed. Only
        # a genuinely different label (not just a different case) replaces it.
        prior_label = existing.get("label", "")
        display_label = prior_label if prior_label.lower() == key else label

        store["periods"][key] = {
            "key": key,
            "label": display_label or label,

            "start": s.isoformat(),
            "end": e.isoformat(),
            "note": (
                " ".join(note.split())[:300] if note is not None
                else existing.get("note", "")
            ),
            "set_by": set_by or existing.get("set_by", ""),
            "set_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _save(store)
        return store["periods"][key]



def list_periods() -> List[dict]:
    """Admin-entered periods, chronological — the order a calendar is read in."""
    items = list(_load().get("periods", {}).values())
    items.sort(key=lambda p: p.get("start", ""))
    return items


def delete_period(key: str) -> bool:
    with _lock:
        store = _load()
        if (key or "").strip().lower() in store.get("periods", {}):
            del store["periods"][key.strip().lower()]
            _save(store)
            return True
    return False


# ---------------------------------------------------------------------------
# Where are we in the calendar?
# ---------------------------------------------------------------------------

# A deadline stops being "upcoming" and becomes something to warn about at this
# distance. Two weeks is enough time to act on enrollment or a payment and short
# enough that the warning still feels specific.
SOON_DAYS = 14


def classify(period: dict, today: datetime.date) -> Optional[dict]:
    """
    Add timing to one period: status, and the day distance that matters for it.

    Returns None for a malformed row rather than raising — a bad date in one line
    of a PDF must not take down the whole calendar block.
    """
    try:
        start = datetime.date.fromisoformat(period["start"])
        end = datetime.date.fromisoformat(period.get("end") or period["start"])
    except (KeyError, TypeError, ValueError):
        return None

    out = dict(period)
    if today < start:
        out["status"] = "upcoming"
        out["days_until_start"] = (start - today).days
        out["days_until_end"] = (end - today).days
    elif today > end:
        out["status"] = "past"
        out["days_since_end"] = (today - end).days
    else:
        out["status"] = "active"
        # Days remaining is inclusive of today: on the closing day itself the
        # honest answer is "today is the last day", not "0 days left".
        out["days_left"] = (end - today).days
    out["is_soon"] = out.get("status") == "upcoming" and out.get("days_until_start", 999) <= SOON_DAYS
    out["closing_soon"] = out.get("status") == "active" and out.get("days_left", 999) <= SOON_DAYS
    return out


def current_state(periods: List[dict], today: Optional[datetime.date] = None) -> dict:
    """
    Split periods into active / upcoming / past, each sorted the way a human
    would want to read it.
    """
    today = today or today_in_manila()
    active, upcoming, past = [], [], []
    for p in periods or []:
        c = classify(p, today)
        if not c:
            continue
        {"active": active, "upcoming": upcoming, "past": past}[c["status"]].append(c)

    active.sort(key=lambda p: p.get("days_left", 0))          # closing first
    upcoming.sort(key=lambda p: p.get("days_until_start", 0))  # soonest first
    past.sort(key=lambda p: p.get("days_since_end", 0))        # most recent first
    return {
        "today": today.isoformat(),
        "active": active,
        "upcoming": upcoming,
        "past": past,
    }


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

# Only questions that are actually about timing get a calendar block. Everything
# else (programs, fees, requirements) would just be paying tokens for context the
# answer does not use.
_TIME_WORDS = re.compile(
    r"\b(when|deadline|due|schedule|sched|start|starts|begin|begins|end|ends|"
    r"open|opens|close|closes|closing|last day|until|still|late|miss|missed|"
    r"enroll|enrolment|enrollment|register|registration|exam|exams|semester|"
    r"sem|term|break|holiday|vacation|graduation|payment|pay|today|tomorrow|"
    r"kailan|hanggang|bukas|ngayon|huli|deadline na)\b",
    re.I,
)


def is_time_sensitive(question: str) -> bool:
    """True when the question is about timing and a calendar block would help."""
    return bool(_TIME_WORDS.search(question or ""))


def _human(d: str) -> str:
    """
    "2026-06-15" -> "June 15, 2026".

    Built from the parts instead of `strftime("%B %-d")` because `%-d` is a
    glibc extension: it silently produces a literal "%-d" on Windows, which would
    put `June %-d, 2026` in front of a student. Zero-padding is stripped by
    construction rather than by a string replace, so a date like "June 10" does
    not lose its own zero.
    """
    try:
        dt = datetime.date.fromisoformat(d)
    except (TypeError, ValueError):
        return d
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"



def calendar_block(
    question: str,
    *,
    periods: Optional[List[dict]] = None,
    today: Optional[datetime.date] = None,
    max_items: int = 6,
) -> str:
    """
    Render the timing context for one question, or "" when it does not apply.

    The wording is deliberately conclusive — "OPEN, closes in 12 days" rather than
    a pair of dates — because the arithmetic is the part the model gets wrong, and
    a computed sentence cannot be miscomputed downstream.
    """
    if not is_time_sensitive(question):
        return ""

    today = today or today_in_manila()
    state = current_state(periods if periods is not None else list_periods(), today)

    if not (state["active"] or state["upcoming"]):
        # Nothing live. Saying so is still worth the tokens: it stops the model
        # reciting a past date in the present tense, which is the failure that
        # sends a student to campus on the wrong day.
        if state["past"]:
            recent = state["past"][0]
            return (
                "<calendar>\n"
                f"Today is {_human(state['today'])} (Samar College local time).\n"
                f"No academic period is currently open. The most recent one, "
                f"{recent['label']}, ended {recent['days_since_end']} day(s) ago on "
                f"{_human(recent['end'])}.\n"
                "If the student asks about a date that has passed, say clearly that it "
                "has passed and do NOT describe it as upcoming.\n"
                "</calendar>"
            )
        return ""

    lines = [
        "<calendar>",
        f"Today is {_human(state['today'])} (Samar College local time).",
        "The timing below is computed from the official calendar. Use these words "
        "for anything about dates — do NOT recompute how many days are left, and "
        "do NOT describe a past date as upcoming.",
        "",
    ]

    for p in state["active"][:max_items]:
        left = p.get("days_left", 0)
        if left == 0:
            when = f"TODAY IS THE LAST DAY (ends {_human(p['end'])})"
        elif p.get("closing_soon"):
            when = f"OPEN NOW but closes in {left} day(s), on {_human(p['end'])}"
        else:
            when = f"OPEN NOW until {_human(p['end'])} ({left} day(s) left)"
        lines.append(f"- {p['label']}: {when}")
        if p.get("note"):
            lines.append(f"  note: {p['note']}")

    for p in state["upcoming"][:max_items]:
        days = p.get("days_until_start", 0)
        if p.get("is_soon"):
            when = f"NOT YET OPEN — starts in {days} day(s), on {_human(p['start'])}"
        else:
            when = f"NOT YET OPEN — starts {_human(p['start'])} (in {days} day(s))"
        lines.append(f"- {p['label']}: {when}")
        if p.get("note"):
            lines.append(f"  note: {p['note']}")

    # One recently-ended period, because "did I miss it?" is the other half of
    # every timing question and the answer has to be allowed to say yes.
    if state["past"]:
        recent = state["past"][0]
        if recent.get("days_since_end", 999) <= 30:
            lines.append(
                f"- {recent['label']}: ALREADY ENDED "
                f"{recent['days_since_end']} day(s) ago on {_human(recent['end'])}"
            )

    lines.append("</calendar>")
    return "\n".join(lines)
