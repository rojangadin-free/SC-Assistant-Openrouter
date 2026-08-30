"""
verify_chunking.py — inspect how a document will be chunked BEFORE indexing.

This is the fast feedback loop for "the AI can't find X in my PDF" bugs:
it runs the real extraction + cleaning + merging + chunking pipeline offline
(no Pinecone, no embeddings, no API calls) and shows you the chunks.

Usage
-----
  # Show which chunks contain a keyword
  python tools/verify_chunking.py "C:\\path\\to\\file.pdf" --grep SCTI

  # Inspect specific pages
  python tools/verify_chunking.py data/Samar-College-2024.pdf --pages 42-44

  # Dump every chunk (long!)
  python tools/verify_chunking.py data/Samar-College-2024.pdf --all
"""

# Make the repo root importable and force the CWD there: this script lives in
# tools/ but every path and import below assumes the repo root. Must come
# before the first repo import.
import _bootstrap  # noqa: F401

import argparse
import logging
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fitz  # PyMuPDF
from langchain_core.documents import Document

from src.helper import clean_text
from store_index import _chunk_and_dedup


def parse_pages(spec: str):
    """'14-16,20' -> {14, 15, 16, 20}"""
    pages = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return pages


def load_pdf_pages(path: str, filename: str, only_pages=None):
    """Text-layer extraction only (no OCR / no vision calls)."""
    docs = []
    with fitz.open(path) as doc:
        for i in range(len(doc)):
            page_num = i + 1
            if only_pages and page_num not in only_pages:
                continue
            text = clean_text(doc.load_page(i).get_text("text") or "")
            if text.strip():
                docs.append(
                    Document(
                        page_content=text,
                        metadata={"source": filename, "page": page_num, "type": "pdf"},
                    )
                )
    return docs


def main():
    ap = argparse.ArgumentParser(description="Preview chunking for a PDF.")
    ap.add_argument("path", help="Path to the PDF")
    ap.add_argument("--pages", help="Page range, e.g. 14-16 or 14,15,20")
    ap.add_argument("--grep", help="Only show chunks containing this text (case-insensitive)")
    ap.add_argument("--all", action="store_true", help="Print every chunk in full")
    ap.add_argument("--verbose", action="store_true", help="Show merge decisions")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )

    filename = args.path.replace("\\", "/").rsplit("/", 1)[-1]
    only_pages = parse_pages(args.pages) if args.pages else None

    raw_docs = load_pdf_pages(args.path, filename, only_pages)
    print(f"Loaded {len(raw_docs)} page(s) from {filename}")

    chunks = _chunk_and_dedup(raw_docs)
    print(f"Produced {len(chunks)} chunk(s)\n")

    shown = 0
    for i, ch in enumerate(chunks):
        body = ch.page_content
        if args.grep and args.grep.lower() not in body.lower():
            continue

        pg = ch.metadata.get("page", "?")
        pg_end = ch.metadata.get("page_end")
        page_label = f"p{pg}" + (f"-{pg_end}" if pg_end and pg_end != pg else "")
        section = ch.metadata.get("section", "-")

        print("=" * 78)
        print(f"CHUNK {i + 1} | {page_label} | section={section} | {len(body)} chars")
        print("=" * 78)
        if args.all or args.grep:
            print(body)
        else:
            print(body[:600] + ("..." if len(body) > 600 else ""))
        print()
        shown += 1

    if args.grep:
        print(f"--> {shown} chunk(s) contain '{args.grep}'")
        if shown == 0:
            print("    WARNING: keyword not found in any chunk. It will NOT be retrievable.")


if __name__ == "__main__":
    main()
