"""
test_announcements.py — rag/announcements.py, offline.

What is actually worth testing here
-----------------------------------
An announcement is not just another row of text: it OVERRIDES the documents. If
the date window is wrong, the assistant confidently repeats a suspension that
ended last week, or hides one that starts today. So the tests concentrate on the
places where a mistake is invisible in the UI and expensive in the chat:

  1. the live window, inclusive at BOTH ends,
  2. audience filtering (a faculty-only notice must never reach a student),
  3. what does and does not reach the prompt,

plus the ordering rule that decides which notice a student reads first.

Everything runs against the local JSON store, so no AWS and no credentials.

Run:  python tests/test_announcements.py
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import datetime
import os
import sys

# Point the shared store at a scratch file BEFORE importing the module — the
# store path is read at import time, and pointing it anywhere else would edit the
# real announcements.json.
os.environ["ANNOUNCEMENTS_FILE"] = ".test_announcements.json"
os.environ.setdefault("SC_STORE_BACKEND", "local")

from rag import announcements as ann  # noqa: E402


PASS, FAIL = [], []
D = datetime.date


def check(name, got, want):
    if got == want:
        PASS.append(name)
        print(f"  [ok]   {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}\n         got:  {got!r}\n         want: {want!r}")


def check_true(name, got):
    check(name, bool(got), True)


def check_false(name, got):
    check(name, bool(got), False)


def reset():
    """Empty the store between sections so no section passes on rows another one
    left behind."""
    for a in ann.list_announcements():
        ann.delete_announcement(a["key"])


def post(title, body="", **kw):
    """`save_announcement` takes body positionally; almost every test here is
    about dates and audiences rather than body text, so this keeps the noise out
    of the assertions."""
    return ann.save_announcement(title, body, **kw)


def titles(**kw):
    return [a["title"] for a in ann.pinned_announcements(**kw)]


def main():
    print("\n=== 1. Saving and reading back ===")
    reset()

    rec = post(
        "Classes suspended on August 22",
        "All classes and offices are closed.",
        priority="urgent",
        audience="all",
        starts_on="2026-08-22",
        expires_on="2026-08-22",
        pinned=True,
        posted_by="admin@sc.edu",
    )
    check_true("save returns the record", rec is not None)
    check_true("record has a key", bool(rec and rec.get("key")))

    items = ann.list_announcements()
    check("one announcement stored", len(items), 1)
    check("title preserved", items[0]["title"], "Classes suspended on August 22")
    check("priority preserved", items[0]["priority"], "urgent")
    check("pinned preserved", items[0]["pinned"], True)
    check_true("post time recorded", bool(items[0].get("created_at")))

    # Editing must correct in place. Two live rows for one event is precisely the
    # contradiction this whole feature set exists to remove.
    post(
        "Classes suspended on August 22 and 23",
        "",
        key=rec["key"],
        priority="urgent",
        audience="all",
        starts_on="2026-08-22",
        expires_on="2026-08-23",
    )
    items = ann.list_announcements()
    check("editing does not duplicate", len(items), 1)
    check("edit applied", items[0]["expires_on"], "2026-08-23")
    check("original post time survives the edit",
          items[0]["created_at"], rec["created_at"])

    print("\n=== 2. Input that cannot be honoured is refused ===")
    # A blank headline would pin an empty box to every chat; a backwards or
    # unparseable window would produce a notice that either never appears or never
    # goes away. Refusing beats guessing in both cases.
    check("empty title rejected", post("   ", "something"), None)
    check("end before start rejected",
          post("Backwards", "", starts_on="2026-09-10", expires_on="2026-09-01"), None)
    check("non-ISO date rejected", post("Bad date", "", starts_on="22-08-2026"), None)

    print("\n=== 3. The live window, both ends inclusive ===")
    reset()
    post("Enrollment extended", "", priority="important", audience="all",
         starts_on="2026-06-10", expires_on="2026-06-15")

    # The boundary days are the whole point. An exclusive end would take the
    # notice down on the morning of the final day — the day most people ask.
    check("day before start: not shown",
          len(titles(audience="students", today=D(2026, 6, 9))), 0)
    check("first day: shown",
          len(titles(audience="students", today=D(2026, 6, 10))), 1)
    check("middle day: shown",
          len(titles(audience="students", today=D(2026, 6, 12))), 1)
    check("last day: shown",
          len(titles(audience="students", today=D(2026, 6, 15))), 1)
    check("day after end: gone",
          len(titles(audience="students", today=D(2026, 6, 16))), 0)

    print("\n=== 4. No end date means it stays up ===")
    reset()
    post("New library hours", "", audience="all")
    check("open-ended notice still live years later",
          len(titles(audience="students", today=D(2030, 1, 1))), 1)

    print("\n=== 5. Audience filtering ===")
    reset()
    post("Faculty meeting Friday", "", audience="faculty")
    post("Tuition deadline moved", "", audience="students")
    post("Campus wifi maintenance", "", audience="all")

    student = titles(audience="students")
    faculty = titles(audience="faculty")

    # The failure guarded against is a leak, not a miscount: internal staff
    # notices reaching every student who opens the chat.
    check_false("faculty notice hidden from students", "Faculty meeting Friday" in student)
    check_true("student notice reaches students", "Tuition deadline moved" in student)
    check_true("'all' reaches students", "Campus wifi maintenance" in student)
    check_true("faculty notice reaches faculty", "Faculty meeting Friday" in faculty)
    check_true("'all' reaches faculty", "Campus wifi maintenance" in faculty)
    check_false("student-only notice hidden from faculty view",
                "Tuition deadline moved" in faculty)

    print("\n=== 6. Taken down, put back, unpinned ===")
    reset()
    r = post("Cancelled event", "", audience="all")
    ann.set_active(r["key"], False)
    check("taken down: no banner", len(titles(audience="students")), 0)
    check("taken down: absent from the prompt",
          ann.announcement_block(audience="students"), "")

    ann.set_active(r["key"], True)
    check("put back: banner returns", len(titles(audience="students")), 1)

    # Unpinned still reaches the MODEL — it only skips the banner. Two different
    # questions, and they must not share one answer.
    reset()
    post("Registrar closes at 3pm today", "", audience="all", pinned=False)
    check("unpinned draws no banner", len(titles(audience="students")), 0)
    check_true("unpinned still reaches the prompt",
               "Registrar closes at 3pm today"
               in ann.announcement_block(audience="students"))

    print("\n=== 7. Urgency decides reading order ===")
    reset()
    post("Info item", "", priority="info", audience="all")
    post("Urgent item", "", priority="urgent", audience="all")
    post("Important item", "", priority="important", audience="all")

    order = titles(audience="students")
    # A student reads the top notice and often stops there, which makes ordering
    # a correctness concern rather than a cosmetic one.
    check("urgent first", order[0], "Urgent item")
    check("important second", order[1], "Important item")
    check("info last", order[2], "Info item")

    print("\n=== 8. The block handed to the model ===")
    reset()
    post("Classes are suspended tomorrow", "Typhoon signal no. 2.",
         priority="urgent", audience="all", expires_on="2026-08-23")

    block = ann.announcement_block(audience="students", today=D(2026, 8, 22))
    check_true("headline present", "Classes are suspended tomorrow" in block)
    check_true("detail present", "Typhoon signal no. 2." in block)
    check_true("urgency marked", "[URGENT]" in block)
    # Without an explicit override instruction the model treats the notice as one
    # more retrieved passage and may still answer from the handbook schedule.
    check_true("states its authority over the documents", "OVERRIDE" in block)
    # The countdown is spelled out so the model never has to subtract dates.
    check_true("remaining days spelled out", "more day(s)" in block)

    # The failure this prevents: telling students to stay home a week after
    # classes resumed.
    check("expired notice absent from the block",
          ann.announcement_block(audience="students", today=D(2026, 8, 30)), "")

    print("\n=== 9. Nothing posted = nothing added ===")
    reset()
    check("empty store yields an empty block",
          ann.announcement_block(audience="students"), "")

    print("\n=== 10. Stats ===")
    reset()
    post("Live urgent", "", priority="urgent", audience="all", expires_on="2026-12-31")
    post("Old one", "", priority="info", audience="all", expires_on="2020-01-01")
    s = ann.stats(today=D(2026, 6, 1))
    check("one live", s["active"], 1)
    check("one urgent", s["urgent"], 1)
    check("one expired", s["expired"], 1)
    check("two in total", s["total"], 2)

    reset()

    print("\n" + "=" * 62)
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    print("=" * 62)
    if FAIL:
        print("\nFailures:")
        for f in FAIL:
            print("  -", f)
        return 1
    print("\nAnnouncements behave correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
