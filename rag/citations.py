"""
rag/citations.py — turn retrieved documents into the short source list a student
can actually check.

Why this exists
---------------
`docs_to_context()` already labels every chunk with `Source:` and `Page:` for the
LLM's benefit, and the prompt forbids answering from anything else. But the
student never sees any of it, so a correct answer and a confidently wrong one
look identical on screen. Showing the pages the answer came from does three
separate jobs:

  * trust     — "p. 15 of the 2024 handbook" is checkable; a bare claim is not
  * self-service — the student can open the page and read the surrounding rules
  * debugging — when an answer is wrong, the citation names the file to fix,
                which is exactly what Answer Quality (rag/feedback.py) needs to
                blame a document instead of a vibe

Design decisions worth defending
--------------------------------
1. **Cite the documents that were actually sent, not all candidates.** The
   retriever pulls ~25 per query and reranks to 12; citing the whole candidate
   pool would be noise dressed up as rigour.

2. **Group by file, collapse pages.** Twelve chunks are often four pages of one
   PDF. "Samar-College-update.pdf, pp. 14-15" is one honest line; twelve
   near-identical lines train the student to ignore the citation block entirely.

3. **Cap the list.** Beyond ~4 files the citation stops being a pointer and
   becomes a bibliography. The cap is on *files*, not chunks, so a well-sourced
   answer is never truncated to a single page.

4. **Suppress citations on a refusal.** If the assistant said "I don't have that
   information", listing the pages it read implies they answered the question.
   `looks_unanswered()` already exists for the Content Gaps path — reusing it
   keeps the two features consistent by construction.

The output is plain data (list of dicts), not HTML, so the same payload serves
the SSE stream, the stored conversation, and the feedback vote that references
it.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional

# A citation block longer than this stops being read.
MAX_SOURCES = 4

# Per file, how many individual page numbers to name before switching to a range.
MAX_PAGES_PER_SOURCE = 6


def _clean_source(source: str) -> str:
    """
    `s3://bucket/uploads/Samar-College-update.pdf` -> `Samar-College-update.pdf`

    Only the basename is shown: the storage path is an implementation detail and
    leaking the bucket layout into the UI is both noise and a small disclosure.
    """
    s = (source or "").strip()
    if not s:
        return ""
    s = s.replace("\\", "/")
    return s.rsplit("/", 1)[-1] or s


def _page_number(page: Any) -> Optional[int]:
    """
    Page metadata arrives as int, str, float, or None depending on the loader.
    Normalised here so sorting and range-collapsing can assume integers.
    """
    if page is None or page == "":
        return None
    if isinstance(page, bool):
        return None
    if isinstance(page, (int, float)):
        return int(page)
    m = re.search(r"\d+", str(page))
    return int(m.group()) if m else None


def _collapse(pages: List[int]) -> str:
    """
    [14, 15, 16, 20] -> "14-16, 20"

    Consecutive pages become a range because that is how a person would say it,
    and because a PDF section rarely stops at a page boundary.
    """
    if not pages:
        return ""

    runs: List[List[int]] = []
    for p in pages:
        if runs and p == runs[-1][-1] + 1:
            runs[-1].append(p)
        else:
            runs.append([p])

    parts = [str(r[0]) if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in runs]

    if len(parts) > MAX_PAGES_PER_SOURCE:
        parts = parts[:MAX_PAGES_PER_SOURCE] + ["…"]

    return ", ".join(parts)


def build_citations(docs: Iterable[Any], *, max_sources: int = MAX_SOURCES) -> List[Dict[str, Any]]:
    """
    Group the documents that were sent to the LLM into one entry per file.

    Ordering follows the reranker: the first document is the one the
    cross-encoder judged most relevant, so the file it came from is listed
    first. Sorting alphabetically instead would bury the page that mattered.

    Returns
        [{"source": "Samar-College-update.pdf",
          "pages": [14, 15],
          "pages_label": "14-15",
          "label": "Samar-College-update.pdf, pp. 14-15",
          "chunks": 3}, ...]
    """
    grouped: Dict[str, Dict[str, Any]] = {}

    for d in docs or []:
        md = getattr(d, "metadata", None) or {}
        name = _clean_source(md.get("source", ""))
        if not name:
            continue

        entry = grouped.setdefault(name, {"source": name, "pages": set(), "chunks": 0})
        entry["chunks"] += 1

        pg = _page_number(md.get("page"))
        if pg is not None:
            entry["pages"].add(pg)

    out: List[Dict[str, Any]] = []
    for entry in list(grouped.values())[:max_sources]:
        pages = sorted(entry["pages"])
        label_pages = _collapse(pages)
        if not label_pages:
            label = entry["source"]
        elif len(pages) == 1:
            label = f"{entry['source']}, p. {label_pages}"
        else:
            label = f"{entry['source']}, pp. {label_pages}"

        out.append({
            "source": entry["source"],
            "pages": pages,
            "pages_label": label_pages,
            "label": label,
            "chunks": entry["chunks"],
        })

    return out


def citation_labels(docs: Iterable[Any], *, max_sources: int = MAX_SOURCES) -> List[str]:
    """
    Flat "file, p. N" strings — the shape rag/feedback.py stores with a vote, so
    that a 👎 can be traced back to a document without re-running retrieval.
    """
    return [c["label"] for c in build_citations(docs, max_sources=max_sources)]
