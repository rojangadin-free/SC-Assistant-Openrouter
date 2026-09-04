"""
backfill_doc_dates.py — give every document already in the corpus an effective
date, so "prefer the newer document" starts working.

The problem this fixes
----------------------
`rag/freshness.py` was complete and correct and did nothing. Its dates are only
recorded by `set_doc_date()`, which was only ever called from the admin upload
preview — so the documents that were seeded into `data/` and indexed directly had
no date at all:

    >>> list_docs()
    []

Two files in the corpus, zero dated. Every downstream consumer degraded silently
rather than failing:

    freshness_block(...)                      -> ""      (on every answer)
    compare("...update.pdf", "...2024.pdf")   -> "no effective date on record"

So when the two handbooks disagreed about the Dean of the College of Education,
nothing told the model that one of them superseded the other, and the answer went
to whichever chunk the reranker happened to score higher. That is the coin flip
the freshness module exists to replace.

`store_index.py` now dates documents as it indexes them, which fixes this for
every future upload and re-index. This script is for the documents that are
ALREADY indexed, so you do not have to re-index a 190-page handbook just to
record a date that is written on its own first page.

Usage
-----
  python tools/backfill_doc_dates.py                 # scan data/, show a plan
  python tools/backfill_doc_dates.py --apply         # actually record them
  python tools/backfill_doc_dates.py --s3 --apply    # the live S3 corpus
  python tools/backfill_doc_dates.py --file data/Samar-College-update.pdf --apply

Dry run by default. Recording a date changes which document the assistant treats
as current, so the default is to print what WOULD change and let you read it
first.

What it will not do
-------------------
- It never overwrites a date an admin set by hand (`date_source == "admin"`).
  A human who corrected a wrong guess must not have it undone by a maintenance
  script.
- It never invents a date for a document that does not state one. An undated file
  stays undated and is reported as such: it cannot win or lose a freshness
  comparison, and the only real fix is a human setting the date. Defaulting it to
  "today" would make an uploaded scan of a 2019 memo instantly outrank the current
  handbook, and the wrongness would look exactly like correctness.
"""

from __future__ import annotations

# Repo root on sys.path and as the CWD: this lives in tools/ but every path
# below assumes the root. Must precede the first repo import.
import _bootstrap  # noqa: F401

import argparse
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from rag.freshness import (  # noqa: E402  (after the stdout reconfigure)
    get_doc_date,
    infer_effective_date,
    list_docs,
    set_doc_date,
)

DATA_DIR = "data"
READABLE_EXT = (".pdf", ".docx", ".doc", ".txt", ".md")

# Only the front of a document makes claims about itself; `date_from_text()`
# reads the first 4000 characters and a date deeper in the body is far more
# likely to be a deadline or an event. Six pages is comfortably more than that
# and avoids opening a 190-page handbook in full to read its cover.
HEAD_PAGES = 6


# ---------------------------------------------------------------------------
# Reading just enough text
# ---------------------------------------------------------------------------

def _head_text_pdf(path: str) -> str:
    import pymupdf

    doc = pymupdf.open(path)
    try:
        pages = [
            doc.load_page(i).get_text("text")
            for i in range(min(HEAD_PAGES, doc.page_count))
        ]
    finally:
        doc.close()
    return "\n".join(pages)


def _head_text_docx(path: str) -> str:
    from docx import Document as DocxDocument

    d = DocxDocument(path)
    out, size = [], 0
    for p in d.paragraphs:
        out.append(p.text)
        size += len(p.text)
        if size > 8000:
            break
    return "\n".join(out)


def _head_text_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(8000)


def head_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            return _head_text_pdf(path)
        if ext in (".docx", ".doc"):
            return _head_text_docx(path)
        if ext in (".txt", ".md"):
            return _head_text_txt(path)
    except Exception as e:
        print(f"  ! could not read {os.path.basename(path)}: {e}")
    return ""


def local_files(target: str):
    if os.path.isfile(target):
        return [target]
    out = []
    for root, _dirs, files in os.walk(target):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in READABLE_EXT:
                out.append(os.path.join(root, f))
    return out


def s3_files(tmpdir: str):
    """Download the live corpus, so the dates match what the assistant serves."""
    import boto3
    from config import S3_BUCKET_NAME

    s3 = boto3.client("s3")
    paths, token = [], None
    while True:
        kw = {"Bucket": S3_BUCKET_NAME}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if os.path.splitext(key)[1].lower() not in READABLE_EXT:
                continue
            dest = os.path.join(tmpdir, os.path.basename(key))
            s3.download_file(S3_BUCKET_NAME, key, dest)
            paths.append(dest)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return paths


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def plan_for(path: str) -> dict:
    """
    What would happen to one file. Nothing is written here.

    `action` is one of:
        "set"     — no date on record, one can be inferred
        "update"  — a date is on record and the inferred one differs
        "same"    — already recorded with this date
        "keep"    — an admin set it; left alone on purpose
        "none"    — no date could be inferred; stays undated
    """
    name = os.path.basename(path)
    existing = get_doc_date(name)
    inferred = infer_effective_date(name, head_text(path))

    if existing and existing.get("date_source") == "admin":
        return {"name": name, "path": path, "action": "keep",
                "current": existing.get("effective_date"),
                "inferred": inferred["date"], "why": "set by an administrator"}

    if not inferred["date"]:
        return {"name": name, "path": path, "action": "none",
                "current": (existing or {}).get("effective_date"),
                "inferred": None, "why": "the document states no date"}

    current = (existing or {}).get("effective_date")
    if current == inferred["date"]:
        action = "same"
    elif current:
        action = "update"
    else:
        action = "set"

    return {"name": name, "path": path, "action": action, "current": current,
            "inferred": inferred["date"], "why": inferred["why"],
            "confidence": inferred["confidence"]}


_SYMBOL = {"set": "+", "update": "~", "same": "=", "keep": "·", "none": "?"}


def main():
    ap = argparse.ArgumentParser(
        description="Record effective dates for documents already in the corpus."
    )
    ap.add_argument("--file", help="One document instead of the whole folder")
    ap.add_argument("--dir", default=DATA_DIR, help=f"Folder (default: {DATA_DIR})")
    ap.add_argument("--s3", action="store_true", help="Use the live S3 corpus")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the dates (default is a dry run)")
    args = ap.parse_args()

    tmpdir = None
    if args.s3:
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="sc_dates_")
        paths = s3_files(tmpdir)
    else:
        paths = local_files(args.file or args.dir)

    if not paths:
        print("No documents found.")
        return 0

    print(f"Reading {len(paths)} document(s)...\n")
    plans = [plan_for(p) for p in paths]

    print("=" * 78)
    print("EFFECTIVE DATES" + ("" if args.apply else "  (dry run — nothing written)"))
    print("=" * 78)
    for p in plans:
        sym = _SYMBOL.get(p["action"], " ")
        date = p["inferred"] or "unknown"
        line = f" {sym} {p['name']:<38} {date:<12} {p['why']}"
        if p["action"] == "update":
            line += f"   (was {p['current']})"
        print(line)

    if args.apply:
        wrote = 0
        for p in plans:
            if p["action"] not in ("set", "update"):
                continue
            try:
                set_doc_date(p["path"], text=head_text(p["path"]), set_by="backfill")
                wrote += 1
            except Exception as e:
                print(f"  ! failed to record {p['name']}: {e}")
        print(f"\nRecorded {wrote} date(s).")
    else:
        pending = sum(1 for p in plans if p["action"] in ("set", "update"))
        if pending:
            print(f"\n{pending} document(s) would be dated. "
                  f"Re-run with --apply to write them.")

    # The ranking as it now stands, because that is the thing the caller
    # actually wants to know: which document wins a contradiction.
    docs = list_docs()
    dated = [d for d in docs if d.get("effective_date")]
    print()
    print("=" * 78)
    print("CURRENT RANKING (newest first)")
    print("=" * 78)
    if not dated:
        print("  Nothing is dated, so no document supersedes any other and the")
        print("  assistant has no basis to prefer one contradicting page over")
        print("  another. This is the state that made the Dean of the College of")
        print("  Education answer flip between runs.")
    for i, d in enumerate(dated):
        tag = "NEWEST" if i == 0 else ""
        print(f"  {d['effective_date']}  {d['filename']:<38} "
              f"{d.get('date_source', ''):<9} {tag}")

    undated = [d["filename"] for d in docs if not d.get("effective_date")]
    missing = [p["name"] for p in plans if p["action"] == "none"]
    for name in sorted(set(undated) | set(missing)):
        print(f"  {'undated':<12} {name:<38} — cannot take part in ranking")

    if tmpdir:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Non-zero when a document in the corpus has no date, so this can gate a
    # deploy the same way tools/check_data_conflicts.py does.
    return 1 if (missing and args.apply) else 0


if __name__ == "__main__":
    sys.exit(main())
