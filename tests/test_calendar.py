"""
test_calendar.py — calendar awareness (rag/calendar.py), offline.

Every assertion pins "today" explicitly. A suite that called `now()` could only
check the branch that happens to be true this week, and the branches that matter
most — the day a period closes, the day after it ends — would go untested until
they broke in front of a student.

Run:  python tests/test_calendar.py
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import datetime
import os
import shutil
import tempfile

_tmp = tempfile.mkdtemp(prefix="sc_calendar_test_")
os.environ["STORE_BACKEND"] = "file"
os.environ["CALENDAR_PERIODS_FILE"] = os.path.join(_tmp, "calendar_periods.json")

import rag.calendar as cal

passed = failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


def section(t):
    print(f"\n=== {t} ===")


D = datetime.date


# ---------------------------------------------------------------------------
section("1. Reading dates out of document text")
# ---------------------------------------------------------------------------

found = cal.parse_periods("Enrollment Period: June 1-15, 2026", default_year=2026)
check("same-month range found", len(found) == 1)
check("start parsed", found and found[0]["start"] == "2026-06-01")
check("end parsed", found and found[0]["end"] == "2026-06-15")
check("label kept", found and "Enrollment" in found[0]["label"])

found = cal.parse_periods("Second Semester: November 3, 2026 - March 20, 2027")
check("cross-month range found", len(found) >= 1)
check("cross-month start", found and found[0]["start"] == "2026-11-03")
check("cross-month end", found and found[0]["end"] == "2027-03-20")

# A range whose first year is omitted and whose months wrap has to borrow the
# previous year, or the period comes out backwards and is silently dropped.
found = cal.parse_periods("Christmas Break: December 20 - January 5, 2027")
check("wrapped range borrows the earlier year", found and found[0]["start"] == "2026-12-20")
check("wrapped range end", found and found[0]["end"] == "2027-01-05")

found = cal.parse_periods("Deadline for Payment: August 30, 2026")
check("single date becomes a one-day period", found and found[0]["start"] == found[0]["end"] == "2026-08-30")

# Feb 31 is a shape match and not a date. Returning it would put a period in the
# store that `classify()` then has to reject on every single request.
check("impossible date ignored", cal.parse_periods("Something: February 31, 2026") == [])
check("no dates -> nothing", cal.parse_periods("The college was founded to serve.") == [])
check("empty text -> nothing", cal.parse_periods("") == [])

# Cross-month must win over same-month: "June 1 – July 15" contains "June 1"
# and taking the same-month reading would end the period in the wrong month.
found = cal.parse_periods("Summer Class: June 1 - July 15, 2026")
check("cross-month beats same-month", any(f["end"] == "2026-07-15" for f in found))


# ---------------------------------------------------------------------------
section("2. Where a date sits relative to a period")
# ---------------------------------------------------------------------------

period = {"label": "Enrollment", "start": "2026-06-01", "end": "2026-06-15"}

c = cal.classify(period, D(2026, 5, 20))
check("before -> upcoming", c["status"] == "upcoming")
check("days until start", c["days_until_start"] == 12)
check("12 days out is 'soon'", c["is_soon"] is True)

c = cal.classify(period, D(2026, 3, 1))
check("far out is not 'soon'", c["is_soon"] is False)

c = cal.classify(period, D(2026, 6, 3))
check("inside -> active", c["status"] == "active")
check("days left", c["days_left"] == 12)
check("closing soon", c["closing_soon"] is True)

c = cal.classify(period, D(2026, 6, 1))
check("first day is active", c["status"] == "active")

# The two boundaries worth being exact about: on the last day the honest answer
# is "today", and one day later it is over. Off by one here and a student is
# told they still have time on the day after the deadline.
c = cal.classify(period, D(2026, 6, 15))
check("last day is still active", c["status"] == "active")
check("last day has 0 days left", c["days_left"] == 0)

c = cal.classify(period, D(2026, 6, 16))
check("day after -> past", c["status"] == "past")
check("days since end", c["days_since_end"] == 1)

check("missing dates -> None, not a crash", cal.classify({"label": "x"}, D(2026, 6, 1)) is None)
check("garbage dates -> None", cal.classify({"start": "not-a-date"}, D(2026, 6, 1)) is None)


# ---------------------------------------------------------------------------
section("3. Grouping and ordering")
# ---------------------------------------------------------------------------

periods = [
    {"label": "Enrollment", "start": "2026-06-01", "end": "2026-06-15"},
    {"label": "Midterms", "start": "2026-08-10", "end": "2026-08-14"},
    {"label": "Orientation", "start": "2026-05-25", "end": "2026-05-26"},
    {"label": "Payment Deadline", "start": "2026-06-30", "end": "2026-06-30"},
]
state = cal.current_state(periods, D(2026, 6, 3))
check("one active", [p["label"] for p in state["active"]] == ["Enrollment"])
check("upcoming soonest-first", [p["label"] for p in state["upcoming"]] == ["Payment Deadline", "Midterms"])
check("past most-recent-first", [p["label"] for p in state["past"]] == ["Orientation"])
check("today echoed", state["today"] == "2026-06-03")
check("empty list is fine", cal.current_state([], D(2026, 6, 3))["active"] == [])


# ---------------------------------------------------------------------------
section("4. Only timing questions get a calendar block")
# ---------------------------------------------------------------------------

check("'when is enrollment' is timing", cal.is_time_sensitive("when is enrollment?") is True)
check("'deadline' is timing", cal.is_time_sensitive("what is the deadline for payment") is True)
check("'still' is timing", cal.is_time_sensitive("can i still enroll") is True)
check("Tagalog 'kailan' is timing", cal.is_time_sensitive("kailan ang enrollment") is True)
check("a dean question is not timing", cal.is_time_sensitive("who is the dean of CITAS?") is False)
check("a program question is not timing", cal.is_time_sensitive("what programs are offered?") is False)
check("empty question is not timing", cal.is_time_sensitive("") is False)

block = cal.calendar_block("who is the dean of CITAS?", periods=periods, today=D(2026, 6, 3))
check("non-timing question costs no tokens", block == "")


# ---------------------------------------------------------------------------
section("5. The block states the conclusion, not the arithmetic")
# ---------------------------------------------------------------------------

block = cal.calendar_block("when is enrollment?", periods=periods, today=D(2026, 6, 3))
check("block rendered", block.startswith("<calendar>") and block.endswith("</calendar>"))
check("today is stated in words", "June 3, 2026" in block)
check("no %-d leaking on Windows", "%-d" not in block)
check("enrollment named", "Enrollment" in block)
check("state is spelled out", "OPEN NOW" in block)
check("days left are pre-computed", "12 day(s)" in block)
check("closing date given", "June 15, 2026" in block)
check("model told not to recount", "do NOT recompute" in block)

# The whole point of the feature: on the last day it must not say "you have time".
block = cal.calendar_block("can i still enroll?", periods=periods, today=D(2026, 6, 15))
check("last day says so", "TODAY IS THE LAST DAY" in block)

block = cal.calendar_block("can i still enroll?", periods=periods, today=D(2026, 6, 20))
check("after the fact, enrollment is not listed as open", "OPEN NOW" not in block)
check("after the fact, it is reported as ended", "ALREADY ENDED" in block)

block = cal.calendar_block("when is enrollment?", periods=periods, today=D(2026, 5, 20))
check("not-yet-open is explicit", "NOT YET OPEN" in block)
check("days until start given", "12 day(s)" in block)

# Nothing live, everything long past: the block still exists, purely to stop the
# model reciting a dead date in the present tense.
old = [{"label": "Enrollment", "start": "2020-06-01", "end": "2020-06-15"}]
block = cal.calendar_block("when is enrollment?", periods=old, today=D(2026, 6, 3))
check("nothing open is said out loud", "No academic period is currently open" in block)
check("and the model is warned about past dates", "has passed" in block)

check("no periods at all -> no block", cal.calendar_block("when is enrollment?", periods=[], today=D(2026, 6, 3)) == "")


# ---------------------------------------------------------------------------
section("6. Admin-entered periods")
# ---------------------------------------------------------------------------

p = cal.set_period("Enrollment", "2026-06-01", "2026-06-15", note="Late fee after June 10", set_by="admin@sc.edu")
check("period saved", p is not None and p["key"] == "enrollment")
check("note kept", p["note"] == "Late fee after June 10")
check("who set it is recorded", p["set_by"] == "admin@sc.edu")

cal.set_period("Midterm Exams", "2026-08-10", "2026-08-14")
check("two periods listed", len(cal.list_periods()) == 2)
check("listed chronologically", [x["label"] for x in cal.list_periods()] == ["Enrollment", "Midterm Exams"])

# Re-saving the same label must correct the row, not add a second one — two rows
# for one event is the contradiction this project already fixed once elsewhere.
cal.set_period("enrollment", "2026-06-01", "2026-06-20")
check("re-saving corrects in place", len(cal.list_periods()) == 2)
check("corrected end date", cal.list_periods()[0]["end"] == "2026-06-20")

# Pushing a date back must not quietly discard the rest of the row. This is the
# bug the suite actually caught: `note` defaulted to "", so "not supplied" and
# "cleared" were indistinguishable and the late-fee warning disappeared from the
# prompt the moment anyone edited the dates.
check("correcting a date keeps the note", cal.list_periods()[0]["note"] == "Late fee after June 10")
check("correcting a date keeps the display label", cal.list_periods()[0]["label"] == "Enrollment")
check("an empty note still clears deliberately",
      cal.set_period("enrollment", "2026-06-01", "2026-06-20", note="")["note"] == "")
cal.set_period("enrollment", "2026-06-01", "2026-06-20", note="Late fee after June 10")


check("end before start rejected", cal.set_period("Bad", "2026-06-15", "2026-06-01") is None)
check("garbage date rejected", cal.set_period("Bad", "next tuesday", "") is None)
check("blank label rejected", cal.set_period("  ", "2026-06-01", "2026-06-15") is None)
check("missing end -> one-day period", cal.set_period("Payment", "2026-06-30", "")["end"] == "2026-06-30")

# A stored period must flow into the prompt with no extra wiring.
block = cal.calendar_block("when is enrollment?", today=D(2026, 6, 3))
check("stored period reaches the prompt", "Enrollment" in block and "OPEN NOW" in block)
check("its note reaches the prompt too", "Late fee" in block)

check("delete works", cal.delete_period("payment") is True)
check("deleting twice is not an error", cal.delete_period("payment") is False)
check("survives a reload", len(cal.list_periods()) == 2)


# ---------------------------------------------------------------------------
section("7. Campus time, not server time")
# ---------------------------------------------------------------------------

check("timezone is +08:00", cal.MANILA.utcoffset(None) == datetime.timedelta(hours=8))
check("today is a date", isinstance(cal.today_in_manila(), datetime.date))

# 16:00 UTC is already tomorrow in Manila. A UTC server would compute "days
# left" against yesterday's date for the last eight hours of every day — exactly
# when a student checks the night before a deadline.
utc_evening = datetime.datetime(2026, 6, 14, 16, 30, tzinfo=datetime.timezone.utc)
check("late UTC is already the next campus day",
      utc_evening.astimezone(cal.MANILA).date() == D(2026, 6, 15))


print("\n" + "=" * 46)
print(f"  {passed} passed, {failed} failed")
print("=" * 46)

shutil.rmtree(_tmp, ignore_errors=True)
raise SystemExit(1 if failed else 0)
