"""
check_data_conflicts.py — CLI data-quality checker for the knowledge base.

Reads the source documents (local `data/` folder by default, or S3 with
`--s3`), extracts role assertions, and prints every `(role, subject)` that the
documents disagree about — the exact class of bug that makes the assistant flip
between two answers for "who is the dean of the College of Education?".

Usage
-----
  python tools/check_data_conflicts.py                       # scan data/
  python tools/check_data_conflicts.py --file data/Samar-College-update.pdf
  python tools/check_data_conflicts.py --s3                  # scan the live S3 corpus
  python tools/check_data_conflicts.py --all-facts           # dump every fact found
  python tools/check_data_conflicts.py --json                # machine-readable output

Exit code is 1 when unresolved conflicts exist, so this can gate a deploy or a
re-index (`python tools/check_data_conflicts.py && python store_index.py`).
"""

from __future__ import annotations

# Make the repo root importable and force the CWD there: this script lives in
# tools/ but every path and import below assumes the repo root. Must come
# before the first repo import.
import _bootstrap  # noqa: F401

import argparse
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from rag.conflicts import (  # noqa: E402  (after stdout reconfigure)
    extract_facts,
    find_conflicts,
    list_resolutions,
)

DATA_DIR = "data"


# ---------------------------------------------------------------------------
# Loading text
#
# Page-level text is used on purpose (not the RAG chunks): a conflict is a
# property of the DOCUMENT, and reading pages keeps this script usable even if
# the chunker changes.
# ---------------------------------------------------------------------------

def _iter_pdf_pages(path: str):
    import pymupdf

    doc = pymupdf.open(path)
    name = os.path.basename(path)
    for i in range(doc.page_count):
        yield name, i + 1, doc.load_page(i).get_text("text")
    doc.close()


def _iter_docx_pages(path: str):
    from docx import Document as DocxDocument

    d = DocxDocument(path)
    name = os.path.basename(path)
    text = "\n".join(p.text for p in d.paragraphs)
    yield name, None, text


def _iter_txt_pages(path: str):
    name = os.path.basename(path)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        yield name, None, fh.read()


def iter_pages(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        yield from _iter_pdf_pages(path)
    elif ext in (".docx", ".doc"):
        yield from _iter_docx_pages(path)
    elif ext in (".txt", ".md"):
        yield from _iter_txt_pages(path)
    # Anything else (images, archives) has no extractable role text.


def local_files(target: str):
    if os.path.isfile(target):
        return [target]
    out = []
    for root, _dirs, files in os.walk(target):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in (".pdf", ".docx", ".doc", ".txt", ".md"):
                out.append(os.path.join(root, f))
    return out


def s3_files(tmpdir: str):
    """Download the live corpus so the check runs against what the AI actually sees."""
    import boto3
    from config import S3_BUCKET_NAME

    s3 = boto3.client("s3")
    paths = []
    token = None
    while True:
        kw = {"Bucket": S3_BUCKET_NAME}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if os.path.splitext(key)[1].lower() not in (".pdf", ".docx", ".doc", ".txt", ".md"):
                continue
            dest = os.path.join(tmpdir, os.path.basename(key))
            s3.download_file(S3_BUCKET_NAME, key, dest)
            paths.append(dest)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return paths


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(conflicts, facts, show_all_facts: bool):
    resolved = list_resolutions()

    print("=" * 78)
    print("DATA CONFLICT REPORT")
    print("=" * 78)
    print(f"Facts extracted : {len(facts)}")
    print(f"Conflicting keys: {len(conflicts)}")
    print(f"Already resolved: {sum(1 for c in conflicts if c.key in resolved)}")
    print()

    if not conflicts:
        print("No contradictions found. The corpus states one value per role.")
    for c in conflicts:
        state = "RESOLVED" if c.key in resolved else "NEEDS REVIEW"
        print("-" * 78)
        print(f"[{state}] {c.role.title()} of {c.subject_display}")
        if c.key in resolved:
            r = resolved[c.key]
            print(f"    -> admin says: {r['correct']}"
                  + (f"   (note: {r['note']})" if r.get("note") else ""))
        for person, occs in sorted(c.values.items(), key=lambda kv: -len(kv[1])):
            where = ", ".join(
                f"{o['source']}" + (f" p{o['page']}" if o["page"] else "")
                for o in occs
            )
            print(f"    * {occs[0]['person_display'] or person.title()}  [{len(occs)}x]  {where}")
            for o in occs[:2]:
                print(f"        \"{o['evidence']}\"")

    if show_all_facts:
        print()
        print("=" * 78)
        print("ALL EXTRACTED FACTS")
        print("=" * 78)
        for f in sorted(facts, key=lambda f: (f.role, f.subject, f.person)):
            loc = f"{f.source}" + (f" p{f.page}" if f.page else "")
            print(f"{f.role:>12} | {f.subject:<40} | {f.person:<28} | {loc}")

    unresolved = [c for c in conflicts if c.key not in resolved]
    if unresolved:
        print()
        print(f"⚠️  {len(unresolved)} unresolved conflict(s). "
              f"Resolve them in Dashboard → Data Conflicts, then re-run this check.")
    return unresolved


def main():
    ap = argparse.ArgumentParser(description="Find contradictory facts in the knowledge base.")
    ap.add_argument("--file", help="Check a single document instead of the whole folder")
    ap.add_argument("--dir", default=DATA_DIR, help=f"Folder to scan (default: {DATA_DIR})")
    ap.add_argument("--s3", action="store_true", help="Scan the live S3 corpus")
    ap.add_argument("--all-facts", action="store_true", help="Also print every extracted fact")
    ap.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON")
    args = ap.parse_args()

    tmpdir = None
    if args.s3:
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="sc_conflict_")
        paths = s3_files(tmpdir)
    else:
        paths = local_files(args.file or args.dir)

    if not paths:
        print("No documents found to check.")
        return 0

    facts = []
    for p in paths:
        for source, page, text in iter_pages(p):
            facts.extend(extract_facts(text, source=source, page=page))

    conflicts = find_conflicts(facts)

    if args.as_json:
        from rag.conflicts import explain_for_admin
        print(json.dumps({
            "documents": [os.path.basename(p) for p in paths],
            "fact_count": len(facts),
            "conflicts": explain_for_admin(conflicts),
        }, indent=2, ensure_ascii=False))
        unresolved = [c for c in conflicts if c.key not in list_resolutions()]
    else:
        print(f"Scanned {len(paths)} document(s): "
              f"{', '.join(os.path.basename(p) for p in paths)}\n")
        unresolved = print_report(conflicts, facts, args.all_facts)

    if tmpdir:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
