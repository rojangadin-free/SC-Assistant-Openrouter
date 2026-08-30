"""
test_freshness.py — document effective dates and the pre-index upload scan.

Run:  python tests/test_freshness.py

The claims worth pinning down:

  1. An undated document gets `None`, **never "today"**. This is the whole safety
     property of the module: default an undated 2019 scan to `now` and it
     instantly outranks the current handbook, silently, and the wrongness looks
     exactly like correctness.

  2. `compare()` refuses to guess. Unknown or equal dates return `newer: None`
     with a reason, so the tie goes to a human instead of to whichever file was
     uploaded second.

  3. The upload scan actually catches the real bug — the two Deans of the College
     of Education — *before* indexing, and reports both values with both pages.

  4. Precedence: admin > document text > filename. A wrong guess must be
     correctable, and the correction must stick.

Storage is redirected to a temp file before import, so the real
doc_freshness.json is never touched.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import datetime
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="sc_freshness_test_")
os.environ["STORE_BACKEND"] = "file"
os.environ["DOC_FRESHNESS_FILE"] = os.path.join(_tmp, "doc_freshness.json")
os.environ["CONFLICT_RESOLUTIONS_FILE"] = os.path.join(_tmp, "conflict_resolutions.json")

# (The old `sys.path.insert(dirname(__file__))` here is gone: it added the repo
#  root only while this file lived in the repo root. _bootstrap above does it
#  correctly from tests/.)

from rag.conflicts import extract_facts                      # noqa: E402
from rag.freshness import (                                   # noqa: E402
    compare, date_from_filename, date_from_text, delete_doc, doc_key,
    freshness_block, get_doc_date, infer_effective_date, list_docs,
    preview_upload, set_doc_date,
)

_passed, _failed = 0, 0


def check(label, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -> {extra}" if extra else ""))


def section(title):
    print(f"\n=== {title} ===")


# ============================================================
section("1. Dates stated inside the document")
# ============================================================
cases = [
    ("STUDENT HANDBOOK\nEffective August 2025\n", datetime.date(2025, 8, 1),
     "effective date"),
    ("Effectivity date: August 1, 2025", datetime.date(2025, 8, 1),
     "effective date"),
    ("Revised: June 2024", datetime.date(2024, 6, 1), "revision date"),
    ("Updated as of March 3, 2026", datetime.date(2026, 3, 3), "revision date"),
    ("Approved January 2025 by the Board", datetime.date(2025, 1, 1),
     "revision date"),
    # A school year opens in June on a Philippine campus — dating this to January
    # would place the document five months before the year it describes.
    ("SAMAR COLLEGE\nS.Y. 2025-2026\n", datetime.date(2025, 6, 1), "school year"),
    ("School Year 2024-2025 Catalog", datetime.date(2024, 6, 1), "school year"),
    ("Academic Year 2025-26", datetime.date(2025, 6, 1), "school year"),
    ("Revised 2024", datetime.date(2024, 1, 1), "year stated"),
]
for text, want, why_frag in cases:
    got, why = date_from_text(text)
    check(f"{text.strip().splitlines()[-1][:38]:40} -> {want}", got == want,
          f"got {got}")
    check(f"   …and it explains itself ({why_frag})", why_frag in why, why)

# Things that must NOT be read as the document's own date.
for text in (
    "Enrollment runs from June 1 to June 15, 2025.",     # a deadline, not a date
    "Room 2024 is on the second floor.",                 # a room number
    "Tuition is PHP 25,000 per semester.",               # money
    "",
):
    got, _ = date_from_text(text)
    check(f"not a document date: {text[:44]!r}", got is None, f"got {got}")

# A date buried deep in the body is a deadline or an event, not the document's
# own effective date.
deep = ("x" * 5000) + "\nEffective August 2025\n"
check("a date past the first 4000 chars is ignored",
      date_from_text(deep)[0] is None, str(date_from_text(deep)[0]))

# An impossible day must degrade to the month rather than raising or inventing.
got, _ = date_from_text("Revised February 30, 2025")
check("Feb 30 falls back to the month", got == datetime.date(2025, 2, 1), str(got))


# ============================================================
section("2. Dates guessed from the filename (weak, and labelled so)")
# ============================================================
got, why = date_from_filename("Samar-College-2024.pdf")
check("a delimited year is read", got == datetime.date(2024, 6, 1), str(got))
check("…and attributed to the filename", "filename" in why, why)

check("a year inside a longer digit run is NOT a year",
      date_from_filename("scan_20240103_002.pdf")[0] is None,
      str(date_from_filename("scan_20240103_002.pdf")[0]))
check("no year at all -> nothing",
      date_from_filename("Samar-College-update.pdf")[0] is None)
check("an implausible year is rejected",
      date_from_filename("report-1789.pdf")[0] is None)
check("the latest year wins when several appear",
      date_from_filename("handbook-2019-rev-2024.pdf")[0] == datetime.date(2024, 6, 1),
      str(date_from_filename("handbook-2019-rev-2024.pdf")[0]))


# ============================================================
section("3. An undated document stays undated — the safety property")
# ============================================================
info = infer_effective_date("Samar-College-update.pdf", "Some text with no date.")
check("date is None, NOT today", info["date"] is None, str(info["date"]))
check("confidence says 'none'", info["confidence"] == "none", info["confidence"])
check("and it does not invent a reason", info["why"] == "", info["why"])

today = datetime.date.today().isoformat()
check("today's date never appears by default", info["date"] != today)


# ============================================================
section("4. Precedence: admin > document > filename")
# ============================================================
info = infer_effective_date(
    "Samar-College-2024.pdf",
    "Effective August 2025",
    stated="2026-01-15",
)
check("the admin's date wins over everything", info["date"] == "2026-01-15",
      info["date"])
check("…and is attributed to the admin", info["source"] == "admin", info["source"])

info = infer_effective_date("Samar-College-2024.pdf", "Effective August 2025")
check("the document beats the filename", info["date"] == "2025-08-01", info["date"])
check("…attributed to the document", info["source"] == "document", info["source"])
check("…with high confidence", info["confidence"] == "high", info["confidence"])

info = infer_effective_date("Samar-College-2024.pdf", "no date here")
check("the filename is the last resort", info["date"] == "2024-06-01", info["date"])
check("…and is marked LOW confidence", info["confidence"] == "low",
      info["confidence"])

info = infer_effective_date("x.pdf", "Effective August 2025", stated="not-a-date")
check("a malformed admin date falls through, it does not blank the result",
      info["date"] == "2025-08-01", info["date"])


# ============================================================
section("5. Storing and correcting a document's date")
# ============================================================
rec = set_doc_date("Samar-College-2024.pdf", text="S.Y. 2024-2025",
                   set_by="admin@sc.edu.ph", note="old handbook")
check("stored", rec["effective_date"] == "2024-06-01", str(rec))
check("who set it is recorded", rec["set_by"] == "admin@sc.edu.ph", rec["set_by"])
check("the note is kept", rec["note"] == "old handbook", rec["note"])
check("first_seen is stamped", bool(rec.get("first_seen")))

first_seen = rec["first_seen"]
rec2 = set_doc_date("Samar-College-2024.pdf", effective_date="2024-08-15",
                    set_by="admin2@sc.edu.ph")
check("a correction overwrites in place", rec2["effective_date"] == "2024-08-15",
      rec2["effective_date"])
check("…and does NOT create a second row", len(list_docs()) == 1,
      str(len(list_docs())))
check("first_seen survives the correction", rec2["first_seen"] == first_seen)

# The same document arrives as a temp path, an S3 key, and a bare name.
check("a full path resolves to the same document",
      get_doc_date("/tmp/upload/Samar-College-2024.pdf") is not None)
check("a windows path too",
      get_doc_date(r"C:\Users\x\Samar-College-2024.pdf") is not None)
check("and case does not matter",
      get_doc_date("samar-college-2024.PDF") is not None)
check("doc_key normalises all three",
      doc_key(r"C:\x\A.PDF") == doc_key("/tmp/a.pdf") == "a.pdf")

try:
    set_doc_date("x.pdf", effective_date="15/01/2026")
    check("a non-ISO date is rejected", False, "no error raised")
except ValueError as e:
    check("a non-ISO date is rejected", "YYYY-MM-DD" in str(e), str(e))

try:
    set_doc_date("")
    check("a blank filename is rejected", False, "no error raised")
except ValueError:
    check("a blank filename is rejected", True)


# ============================================================
section("6. compare() refuses to guess")
# ============================================================
set_doc_date("Samar-College-update.pdf", effective_date="2025-08-01",
             set_by="admin@sc.edu.ph")

cmp = compare("Samar-College-update.pdf", "Samar-College-2024.pdf")
check("the newer document is identified",
      doc_key(cmp["newer"] or "") == "samar-college-update.pdf", str(cmp))
check("…and the older one too",
      doc_key(cmp["older"] or "") == "samar-college-2024.pdf", str(cmp))
check("the reason quotes both dates",
      "2025-08-01" in cmp["reason"] and "2024-08-15" in cmp["reason"],
      cmp["reason"])

# Order of arguments must not change the verdict.
flipped = compare("Samar-College-2024.pdf", "Samar-College-update.pdf")
check("the answer does not depend on argument order",
      doc_key(flipped["newer"] or "") == "samar-college-update.pdf", str(flipped))

cmp = compare("Samar-College-update.pdf", "unknown-file.pdf")
check("an unknown date yields NO winner", cmp["newer"] is None, str(cmp))
check("…and says which file is missing a date",
      "unknown-file.pdf" in cmp["reason"], cmp["reason"])

set_doc_date("twin-a.pdf", effective_date="2025-08-01")
set_doc_date("twin-b.pdf", effective_date="2025-08-01")
cmp = compare("twin-a.pdf", "twin-b.pdf")
check("equal dates yield no winner either", cmp["newer"] is None, str(cmp))
check("…and the reason says they are equal", "both" in cmp["reason"], cmp["reason"])
delete_doc("twin-a.pdf")
delete_doc("twin-b.pdf")


# ============================================================
section("7. The real bug, caught BEFORE indexing")
# ============================================================
# What is already in the corpus: the 2024 handbook names Torremoro. The name and
# the role sit on separate lines, which is how `extract_facts()` actually sees a
# signature block in the real PDF — flattening it to one line would test a shape
# the corpus does not contain.
existing = extract_facts(
    "Dr. Nimfa T. Torremoro\n"
    "Dean, College of Graduate Studies & College of Education\n",
    source="Samar-College-2024.pdf", page=146,
)
check("the existing corpus yielded facts", len(existing) == 2, str(len(existing)))
# One person holding the same role in two units is NOT a conflict, but it must be
# recorded against both, or the real disagreement on one of them stays invisible.
check("…one per unit named in the line",
      sorted(f.subject for f in existing)
      == ["college education", "college graduate studies"],
      str(sorted(f.subject for f in existing)))


# What is being uploaded: the update names Montalis for the same role.
incoming_pages = [
    ("STUDENT HANDBOOK\nEffective August 2025\n", 1),
    ("DEANS UPDATE\nJacqueline Montalis - College of Education\n"
     "Maria Cruz - College of Engineering\n", 15),
]
report = preview_upload("Samar-College-update.pdf", incoming_pages, existing)

check("the incoming date was read from the document",
      report["incoming_date"]["date"] == "2025-08-01",
      str(report["incoming_date"]))
check("exactly one contradiction found", report["summary"]["conflicts"] == 1,
      str(report["summary"]))

c = report["conflicts"][0]
check("it is the College of Education deanship",
      "education" in c["key"].lower(), c["key"])
check("the label is readable", c["label"].startswith("Dean of"), c["label"])
check("the incoming value is Montalis",
      "montalis" in c["incoming"]["value"].lower(), str(c["incoming"]))
check("the existing value is Torremoro",
      "torremoro" in c["existing"]["value"].lower(), str(c["existing"]))
check("the incoming page is cited", 15 in c["incoming"]["pages"],
      str(c["incoming"]["pages"]))
check("the existing page is cited", 146 in c["existing"]["pages"],
      str(c["existing"]["pages"]))
check("the existing source file is named",
      "Samar-College-2024.pdf" in c["existing"]["sources"], str(c["existing"]))
check("freshness identifies the uploaded file as newer",
      c["newer"] == "Samar-College-update.pdf", str(c["newer"]))
check("…and explains why with both dates",
      "2025-08-01" in c["reason"] and "2024-08-15" in c["reason"], c["reason"])
check("the recommendation is advice, not an action",
      "confirm" in c["recommendation"].lower(), c["recommendation"])

check("the engineering dean is reported as NEW, not as a conflict",
      any("engineering" in n["key"] for n in report["new_facts"]),
      str(report["new_facts"]))
check("nothing was auto-resolved: the report only describes",
      "resolved" not in report and "applied" not in report)


# ============================================================
section("8. Agreement is reported too — evidence the upload is sane")
# ============================================================
same = preview_upload(
    "Samar-College-update.pdf",
    [("Dr. Nimfa T. Torremoro\nDean, College of Education\n", 3)],
    existing,
)

check("no conflict when the value matches", same["summary"]["conflicts"] == 0,
      str(same["summary"]))
check("it is counted as an agreement", same["summary"]["agreements"] == 1,
      str(same["summary"]))
check("…and the confirmed value is shown",
      "torremoro" in same["agreements"][0]["value"].lower(),
      str(same["agreements"]))

empty = preview_upload("blank.pdf", [("Nothing of interest here.", 1)], existing)
check("a document with no facts is not an error",
      empty["summary"]["facts_found"] == 0, str(empty["summary"]))
check("…and reports no conflicts", empty["summary"]["conflicts"] == 0)


# ============================================================
section("9. When freshness cannot decide, it says so")
# ============================================================
undated = preview_upload("mystery-memo.pdf", incoming_pages[1:], existing)
check("an undated upload gets no date",
      undated["incoming_date"]["date"] is None, str(undated["incoming_date"]))
check("the conflict is still reported", undated["summary"]["conflicts"] == 1,
      str(undated["summary"]))
u = undated["conflicts"][0]
check("but no winner is declared", u["newer"] is None, str(u["newer"]))
check("…and the advice asks the admin to decide",
      "no effective date" in u["recommendation"].lower()
      or "pin" in u["recommendation"].lower(), u["recommendation"])


# ============================================================
section("10. The prompt block earns its tokens")
# ============================================================
block = freshness_block("Samar-College-update.pdf", "Samar-College-2024.pdf")
check("a block is emitted for two differently-dated sources", bool(block))
check("it is tagged for the model", "<document_dates>" in block)
check("the newest is marked", "NEWEST" in block, block)
check("the oldest is marked", "OLDEST" in block, block)
check("the model is told to prefer the later date",
      "LATER" in block, block)
check("the newest file is listed first",
      block.index("Samar-College-update.pdf") < block.index("Samar-College-2024.pdf"),
      block)

check("one source alone earns no block",
      freshness_block("Samar-College-update.pdf") == "")
check("an unknown source earns no block",
      freshness_block("unknown-a.pdf", "unknown-b.pdf") == "")
check("the same file twice earns no block",
      freshness_block("Samar-College-2024.pdf", "samar-college-2024.pdf") == "")

set_doc_date("same-date-a.pdf", effective_date="2025-08-01")
check("two sources sharing one date earn no block",
      freshness_block("same-date-a.pdf", "Samar-College-update.pdf") == "",
      freshness_block("same-date-a.pdf", "Samar-College-update.pdf"))
check("no sources at all is safe", freshness_block() == "")


# ============================================================
section("11. Listing and deleting")
# ============================================================
set_doc_date("undated.pdf", text="no date in here")
docs = list_docs()
check("the undated document is still listed",
      any(d["key"] == "undated.pdf" for d in docs), str([d["key"] for d in docs]))
dated = [d for d in docs if d.get("effective_date")]
check("dated documents sort newest-first",
      [d["effective_date"] for d in dated]
      == sorted((d["effective_date"] for d in dated), reverse=True),
      str([(d["key"], d.get("effective_date")) for d in docs]))
# `same-date-a.pdf` and `Samar-College-update.pdf` share 2025-08-01. A tie has to
# break on something stable, or the file list would shuffle between page loads
# with nothing having changed.
check("a tie breaks on the filename, not on dict order",
      [d["key"] for d in dated if d["effective_date"] == "2025-08-01"]
      == sorted((d["key"] for d in dated if d["effective_date"] == "2025-08-01"),
                reverse=True),
      str([d["key"] for d in dated if d["effective_date"] == "2025-08-01"]))

check("the undated one sorts last", docs[-1]["key"] == "undated.pdf",
      docs[-1]["key"])

check("delete works", delete_doc("undated.pdf") is True)
check("deleting twice is False, not an exception", delete_doc("undated.pdf") is False)
check("it is gone from the list",
      not any(d["key"] == "undated.pdf" for d in list_docs()))


print("\n" + "=" * 60)
print(f"  {_passed} passed, {_failed} failed")
print("=" * 60)
raise SystemExit(1 if _failed else 0)
