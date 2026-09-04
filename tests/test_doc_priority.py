"""
test_doc_priority.py — which document the assistant treats as CURRENT.

What this suite is guarding
---------------------------
`rag/chain.py` used to decide precedence like this:

    is_base = "samar-college-2024.pdf" in source.lower()
    priority_tag = "" if is_base else " [NEW UPDATE - OVERRIDE BASE KNOWLEDGE]"

It reads as a recency rule. The actual rule is "is not called
samar-college-2024.pdf", and every failure follows from that gap: upload a scan of
a 2019 memo and it is tagged as overriding current policy; rename the handbook and
the base case silently inverts; add `Samar-College-update-2026.pdf` and it is
ranked against a string literal instead of against 2024's real date.

Precedence is now derived from recorded effective dates. The tests below pin the
three properties that make that trustworthy:

  1. the newer document wins, including via a year in the FILENAME
  2. a document with no date NEVER wins, and never loses either
  3. nothing is labelled authoritative when there is nothing to compare it to

Point 2 is the one worth the most attention. The tempting shortcut for an undated
file is to fall back to "today", which would rank a freshly uploaded 2019 memo
above the current handbook — and that wrongness is invisible, because a confident
answer from the wrong document looks exactly like a confident answer.

Storage is redirected to a temp file BEFORE `rag.freshness` is imported, because
the store path is read at import time. No AWS credentials, no Pinecone, no
network.
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must precede the first `rag.freshness` import.
#
# Seeded with an empty store rather than left zero-length: the store logs
# "could not read ..." on a 0-byte file, which is correct behaviour but looks
# like a failure in the middle of a passing run.
_tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
_tmp.write('{"docs": {}, "updated_at": ""}')
_tmp.close()
os.environ["DOC_FRESHNESS_FILE"] = _tmp.name


from rag.freshness import (  # noqa: E402
    compare,
    date_from_filename,
    delete_doc,
    ensure_doc_date,
    get_doc_date,
    infer_effective_date,
    set_doc_date,
    source_priority,
)

passed = 0
failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")


def check_true(label, got):
    check(label, bool(got), True)


def reset():
    """Empty the store between sections so one section cannot mask another."""
    for name in list_names():
        delete_doc(name)


def list_names():
    from rag.freshness import list_docs
    return [d["filename"] for d in list_docs()]


# ===========================================================================
print("\n--- A year in the filename is a real signal ---")
# ===========================================================================

check("a delimited year is read",
      date_from_filename("samar-college-update-2026.pdf")[0].isoformat(),
      "2026-06-01")

check("the HIGHEST year wins when a name carries two",
      date_from_filename("handbook-2019-revised-2026.pdf")[0].isoformat(),
      "2026-06-01")

# June, not January: a Philippine academic year opens mid-year, so dating
# "2026" to January would place the file five months before the year it
# describes and could lose a comparison against the previous S.Y.
check("the month is June, not January",
      date_from_filename("x-2026.pdf")[0].month, 6)

# A digit run is not a claim about a school year. Without this guard, every
# camera-roll filename and scanner output parses as a date.
check("a digit RUN is not a year (scan_20240103_002.pdf)",
      date_from_filename("scan_20240103_002.pdf")[0], None)

check("a year outside the plausible range is rejected",
      date_from_filename("doc-1492.pdf")[0], None)

check("no year at all -> None, not today",
      date_from_filename("Samar-College-update.pdf")[0], None)


# ===========================================================================
print("\n--- The document's own words outrank its filename ---")
# ===========================================================================

# This ordering is why filename parsing is tier 3 and not tier 1, and the real
# corpus is the argument: Samar-College-update.pdf has NO year in its name. Rank
# by filename first and the only dated file becomes the 2024 handbook, so the
# OLDER document would be the one that wins — the exact inversion of the intent.
stated = infer_effective_date(
    "Samar-College-2024.pdf",
    "Student Handbook\nSchool Year 2025-2026\n",
)
check("a stated S.Y. beats the year in the name", stated["date"], "2025-06-01")
check("...and says where it came from", stated["source"], "document")

only_name = infer_effective_date("Samar-College-2024.pdf", "no date in here")
check("filename is used when the text says nothing", only_name["date"], "2024-06-01")
check("...and is labelled low confidence", only_name["confidence"], "low")

nothing = infer_effective_date("handbook.pdf", "no date anywhere")
check("undated stays undated", nothing["date"], None)
check("...and is NOT silently today", nothing["confidence"], "none")


# ===========================================================================
print("\n--- 2026 beats 2024, on filenames alone ---")
# ===========================================================================
reset()

ensure_doc_date("Samar-College-2024.pdf", "handbook body with no stated date")
ensure_doc_date("samar-college-update-2026.pdf", "update body with no stated date")

check("the 2024 file is dated from its name",
      get_doc_date("Samar-College-2024.pdf")["effective_date"], "2024-06-01")
check("the 2026 file is dated from its name",
      get_doc_date("samar-college-update-2026.pdf")["effective_date"], "2026-06-01")

cmp = compare("samar-college-update-2026.pdf", "Samar-College-2024.pdf")
check("the 2026 document is newer",
      os.path.basename(cmp["newer"] or ""), "samar-college-update-2026.pdf")
check("...and the 2024 one is older",
      os.path.basename(cmp["older"] or ""), "Samar-College-2024.pdf")
check_true("...and the reason names both dates",
           "2026-06-01" in cmp["reason"] and "2024-06-01" in cmp["reason"])

# Order of arguments must not decide the winner.
cmp_rev = compare("Samar-College-2024.pdf", "samar-college-update-2026.pdf")
check("the comparison is symmetric",
      os.path.basename(cmp_rev["newer"] or ""), "samar-college-update-2026.pdf")


# ===========================================================================
print("\n--- ensure_doc_date: idempotent, and never overrules a human ---")
# ===========================================================================
reset()

first = ensure_doc_date("Samar-College-2024.pdf", "body")
check("a date is recorded", first["effective_date"], "2024-06-01")

again = ensure_doc_date("Samar-College-2024.pdf", "body")
check("re-indexing does not change it", again["effective_date"], "2024-06-01")

# An admin corrects a wrong guess...
set_doc_date("Samar-College-2024.pdf", effective_date="2030-01-15", set_by="admin")
check("the admin's date is stored",
      get_doc_date("Samar-College-2024.pdf")["effective_date"], "2030-01-15")

# ...and the next re-index must not quietly undo the correction. This is the
# whole reason ensure_doc_date exists instead of calling set_doc_date directly.
ensure_doc_date("Samar-College-2024.pdf", "body")
check("re-indexing does NOT overwrite the admin's date",
      get_doc_date("Samar-College-2024.pdf")["effective_date"], "2030-01-15")
check("...and the source still says admin",
      get_doc_date("Samar-College-2024.pdf")["date_source"], "admin")

# An undated document must not be given a date just because it was indexed.
none_rec = ensure_doc_date("mystery-memo.pdf", "no date in this text")
check("an undated file records nothing", none_rec, None)
check("...and stays absent from the store", get_doc_date("mystery-memo.pdf"), None)


# ===========================================================================
print("\n--- source_priority: only claims precedence when it is true ---")
# ===========================================================================
reset()

ensure_doc_date("Samar-College-2024.pdf", "body")
ensure_doc_date("samar-college-update-2026.pdf", "body")

pri = source_priority("Samar-College-2024.pdf", "samar-college-update-2026.pdf")
check("both documents are ranked", len(pri), 2)
check_true("the 2026 file is the newest",
           pri["samar-college-update-2026.pdf"]["is_newest"])
check_true("the 2024 file is superseded",
           pri["samar-college-2024.pdf"]["is_superseded"])
check("the newest is not also superseded",
      pri["samar-college-update-2026.pdf"]["is_superseded"], False)
check("rank 0 is the newest", pri["samar-college-update-2026.pdf"]["rank"], 0)

# One source cannot supersede anything. The old filename rule tagged it anyway,
# telling the model to override knowledge that nothing had contradicted.
check("a single source gets no precedence label",
      source_priority("samar-college-update-2026.pdf"), {})

# A dated file plus an undated one is not evidence of anything.
check("dated + undated -> no ranking",
      source_priority("Samar-College-2024.pdf", "mystery-memo.pdf"), {})

# Two undated files are the state the whole corpus was in before this change.
reset()
check("two undated files -> no ranking",
      source_priority("a.pdf", "b.pdf"), {})

# Same date on both: there is no newer one, and inventing a distinction would
# invite the model to explain a difference that does not exist.
reset()
set_doc_date("a-2026.pdf", effective_date="2026-06-01", set_by="admin")
set_doc_date("b-2026.pdf", effective_date="2026-06-01", set_by="admin")
check("equal dates -> no ranking", source_priority("a-2026.pdf", "b-2026.pdf"), {})

# The same file under a temp path, an S3 key and a bare name is ONE document.
reset()
ensure_doc_date("Samar-College-2024.pdf", "body")
ensure_doc_date("samar-college-update-2026.pdf", "body")
pri = source_priority(
    "/tmp/upload/Samar-College-2024.pdf",
    "docs/2026/samar-college-update-2026.pdf",
)
check("paths are keyed by basename", len(pri), 2)
check_true("...and still rank correctly",
           pri["samar-college-update-2026.pdf"]["is_newest"])


# ===========================================================================
print("\n--- the hardcoded filename is gone from the prompt path ---")
# ===========================================================================

chain_src = open("rag/chain.py", encoding="utf-8").read()

body = chain_src.split("def docs_to_context", 1)[1].split("\ndef ", 1)[0]

# Only the EXECUTABLE lines are inspected. The docstring and comments quote the
# old rule verbatim — deliberately, because the next person to touch this needs
# to know what was wrong with it — so a naive substring search over the whole
# function would flag the very explanation that prevents the regression.
code = re.sub(r'"""[\s\S]*?"""', "", body)
code = "\n".join(
    ln for ln in code.splitlines() if not ln.strip().startswith("#")
)

check_true("docs_to_context asks source_priority(), not a filename",
           "source_priority(" in code)

# The literal itself. If this comes back, precedence has silently stopped being
# about dates and no behavioural test above would notice: every assertion in
# this file would still pass while the corpus was ranked by a string compare.
check("no hardcoded document name is left in the code",
      "samar-college-2024.pdf" in code.lower(), False)
check("no `is_base` filename comparison remains", "is_base" in code, False)
check_true("the tag names the date when it appears", "effective" in code)


check_true("store_index dates a document as it indexes it",
           "_record_effective_date" in open("store_index.py", encoding="utf-8").read())

store_src = open("store_index.py", encoding="utf-8").read()
check("both indexing paths record a date",
      store_src.count("_record_effective_date(") >= 3, True)


# ===========================================================================
print(f"\n  {passed} passed, {failed} failed")
os.unlink(_tmp.name)
sys.exit(1 if failed else 0)
