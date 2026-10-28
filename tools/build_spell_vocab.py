"""
tools/build_spell_vocab.py — teach the spell checker what the documents say.

    python tools/build_spell_vocab.py            # read data/, write spell_vocab.json
    python tools/build_spell_vocab.py --path docs
    python tools/build_spell_vocab.py --check "latin honir requirements"

Why this exists
---------------
`rag/spelling.py` only ever repairs a word into another word THE DOCUMENTS
CONTAIN. That is the whole reason it is safe to run on every question: it cannot
invent a search term, only recover one. But it needs the list of words the
documents contain, and `bm25_values.json` cannot supply it — pinecone-text
stores hashed token ids and document frequencies, not the tokens themselves, so
the corpus vocabulary is unrecoverable from the index once it is built.

So it is extracted here, straight from the files in `data/`, and written to
`spell_vocab.json` as `{"terms": {word: count}}`.

Text extraction is deliberately cheap
-------------------------------------
No OCR, no vision model, no chunking. This is not indexing — a word that only
appears inside a scanned image is a word this checker can afford not to know,
and paying for OCR here would make refreshing the vocabulary a decision instead
of a habit. `store_index.py` calls `learn_terms()` on the real chunk text when a
document is actually indexed, which is where the OCR'd pages get picked up.

Run it after adding documents to `data/`. Re-running is safe: counts accumulate
into the existing file, and `rag/spelling.py` requires a term to be seen a few
times before it will ever be suggested, so one garbled page cannot become a
correction.
"""

import _bootstrap  # noqa: F401

import argparse
import os
import sys
from typing import List

from rag.spelling import (
    SPELL_VOCAB_FILE,
    learn_terms,
    misspellings,
    reload_vocabulary,
    vocabulary_size,
)

# Extensions we can read without a model. Anything else in data/ is skipped with
# a line saying so, rather than silently — a corpus whose vocabulary is half
# missing produces corrections that look arbitrary.
_TEXT_EXTS = {".txt", ".md", ".csv", ".json"}


def _read_pdf(path: str) -> str:
    try:
        import pymupdf  # noqa: F401  (the modern name for fitz)
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            print("  ! PyMuPDF not installed; PDFs skipped.")
            return ""

    try:
        with pymupdf.open(path) as doc:
            return "\n".join(page.get_text() for page in doc)
    except Exception as e:
        print(f"  ! {os.path.basename(path)}: {e}")
        return ""


def _read_docx(path: str) -> str:
    try:
        import docx
    except ImportError:
        print("  ! python-docx not installed; DOCX skipped.")
        return ""

    try:
        document = docx.Document(path)
        parts: List[str] = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.extend(cell.text for cell in row.cells)
        return "\n".join(parts)
    except Exception as e:
        print(f"  ! {os.path.basename(path)}: {e}")
        return ""


def _read_plain(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError as e:
        print(f"  ! {os.path.basename(path)}: {e}")
        return ""


def read_documents(folder: str) -> List[str]:
    texts: List[str] = []

    if not os.path.isdir(folder):
        print(f"No such folder: {folder}")
        return texts

    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue

        ext = os.path.splitext(name)[1].lower()
        if ext == ".pdf":
            text = _read_pdf(path)
        elif ext == ".docx":
            text = _read_docx(path)
        elif ext in _TEXT_EXTS:
            text = _read_plain(path)
        else:
            print(f"  - {name} (skipped: {ext or 'no extension'})")
            continue

        words = len(text.split())
        print(f"  + {name}: {words:,} words")
        if text:
            texts.append(text)

    return texts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default="data",
                        help="folder of documents to read (default: data)")
    parser.add_argument("--check", metavar="QUESTION",
                        help="report what would be corrected in QUESTION, "
                             "without rebuilding anything")
    args = parser.parse_args()

    if args.check:
        reload_vocabulary()
        print(f"Vocabulary: {vocabulary_size():,} terms "
              f"({SPELL_VOCAB_FILE})")
        repairs = misspellings(args.check)
        if not repairs:
            print(f"'{args.check}' -> no corrections (every word is known)")
        else:
            for typed, meant in repairs:
                print(f"'{args.check}' -> {typed!r} looks like {meant!r}")
        return 0

    print(f"Reading documents from {args.path}/ ...")
    texts = read_documents(args.path)
    if not texts:
        print("Nothing readable found. Vocabulary unchanged.")
        return 1

    before = vocabulary_size()
    after = learn_terms(texts)
    print(f"\nVocabulary written to {SPELL_VOCAB_FILE}: "
          f"{before:,} -> {after:,} terms")

    # A smoke check on the question that motivated the module, so a rebuild that
    # produced a useless vocabulary is visible immediately rather than the next
    # time a student asks.
    probe = "latin honir requirements"
    repairs = misspellings(probe)
    print(f"Check: {probe!r} -> "
          + (", ".join(f"{a} -> {b}" for a, b in repairs) or "no corrections"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
