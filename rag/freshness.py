"""
rag/freshness.py — catch the contradiction at UPLOAD time, and let the newer
document win.

The gap this closes
-------------------
`rag/conflicts.py` already detects that two pages disagree about the Dean of the
College of Education, and an admin can pin the right answer. But that whole loop
starts *after* the damage:

    1. admin uploads a new handbook
    2. it is chunked and indexed immediately
    3. students get contradictory answers for days
    4. someone eventually downvotes
    5. an admin notices the conflict screen and resolves it

Steps 2-4 are the problem. The contradiction is knowable at step 1 — the new
file's facts can be compared against what is already indexed *before* anyone asks
a question. This module does that comparison and hands the admin a decision at
the moment they still have the document open, which is the only moment they
actually know which value is current.

Two things, one idea
--------------------
**Freshness.** Every document gets an effective date, and the assistant is told
which of two contradicting sources is newer. This is the piece that lets the
system take a defensible default instead of a coin flip: when
`Samar-College-2024.pdf` and `Samar-College-update.pdf` disagree, "update"
supersedes "2024" — not because of the words in the filename, but because an
explicit date says so.

**A pre-index scan.** `preview_upload()` extracts facts from the incoming file
and reports which of them contradict the existing corpus, with both values and
both page numbers, before a single vector is written.

Where the date comes from, in order of trust
--------------------------------------------
1. **What the admin typed.** Always wins. A human who knows the document beats
   any heuristic, and there must be a way to override a wrong guess.
2. **A date inside the document text.** "Effective August 2025", "S.Y. 2025-2026",
   "Revised: June 2024" — an explicit claim by the document about itself.
3. **A year in the filename.** `Samar-College-2024.pdf`. Weak but real signal.
4. **Nothing.** Returned as `None`, NOT as "today".

Point 4 is the one that matters. Defaulting an undated file to `now` would make
every freshly uploaded scan of a 2019 memo instantly outrank the current
handbook, silently, and the wrongness would look exactly like correctness. An
unknown date must stay unknown so that `compare()` can say "I cannot tell which
of these is newer" and the admin — not a silent tiebreak — decides.

Deliberately NOT here
---------------------
No "auto-resolve the conflict by picking the newer file". Freshness is *evidence*
for a decision, not the decision. A newer document can be a draft, a partial
erratum, or a scan of something old; the registrar knows, the file's mtime does
not. So this module ranks and explains, and `set_resolution()` in
`rag/conflicts.py` stays the only thing that enforces.
"""

from __future__ import annotations

import datetime
import os
import re
import threading
from typing import Dict, Iterable, List, Optional, Tuple

from rag.store import JsonBlobStore
from rag.conflicts import (
    Fact,
    canonical_role,
    display_name,
    extract_facts,
    normalize_person,
    normalize_subject,
)

# ---------------------------------------------------------------------------
# Storage: per-document metadata (effective date, who set it, how we know).
#
# Separate store from conflict resolutions on purpose. A resolution is a decision
# about one FACT; this is a property of a FILE. Merging them would mean deleting
# a resolved conflict could take a document's date with it.
# ---------------------------------------------------------------------------
DOC_META_FILE = os.getenv("DOC_FRESHNESS_FILE", "doc_freshness.json")


def _empty_store() -> dict:
    return {"docs": {}, "updated_at": ""}


_store_obj: Optional[JsonBlobStore] = None
_store_obj_path: Optional[str] = None
_store_lock = threading.Lock()


def _store():
    """Built lazily so tests can redirect DOC_FRESHNESS_FILE after import."""
    global _store_obj, _store_obj_path
    path = os.getenv("DOC_FRESHNESS_FILE", DOC_META_FILE)
    if _store_obj is None or _store_obj_path != path:
        _store_obj = JsonBlobStore("freshness", path, _empty_store)
        _store_obj_path = path
    return _store_obj


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def doc_key(filename: str) -> str:
    """
    Documents are keyed by basename, lowercased.

    The same file arrives as a temp path at upload, an S3 key in metadata, and a
    bare name in `Document.metadata["source"]`. Keying on anything but the
    basename would file those as three different documents and the freshness of
    the uploaded one would never be found again.
    """
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    return name.lower()


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# Only years a college handbook could plausibly carry. Without this, page numbers,
# room numbers and tuition figures all parse as years.
_YEAR_MIN, _YEAR_MAX = 1990, 2100

_MONTH_ALT = "|".join(_MONTHS)

# Ordered by how strongly the phrasing claims to be the document's own date.
# The first pattern that matches wins, so "Effective August 2025" beats a stray
# "as of 2019" further down the page.
_DATE_PATTERNS: List[Tuple[str, str]] = [
    # "Effective August 2025", "effectivity date: August 1, 2025"
    (r"effectiv(?:e|ity)(?:\s+date)?\s*:?\s*"
     rf"(?:(?P<d1>\d{{1,2}})\s+)?(?P<m1>{_MONTH_ALT})\.?\s+(?:(?P<d2>\d{{1,2}}),?\s+)?(?P<y>\d{{4}})",
     "effective date stated in the document"),
    # "Revised: June 2024", "Updated as of March 3, 2026", "Approved January 2025"
    (r"(?:revis(?:ed|ion)|updated|amended|approved|adopted)\s*"
     r"(?:as\s+of\s*)?:?\s*"
     rf"(?:(?P<d1>\d{{1,2}})\s+)?(?P<m1>{_MONTH_ALT})\.?\s+(?:(?P<d2>\d{{1,2}}),?\s+)?(?P<y>\d{{4}})",
     "revision date stated in the document"),
    # "S.Y. 2025-2026", "School Year 2025-2026", "AY 2025-26"
    (r"(?:s\.?\s*y\.?|a\.?\s*y\.?|school\s+year|academic\s+year)\s*:?\s*"
     r"(?P<y>\d{4})\s*[-–—/]\s*\d{2,4}",
     "school year stated in the document"),
    # A bare "Revised 2024" / "Effective 2025" with no month.
    (r"(?:effectiv(?:e|ity)|revis(?:ed|ion)|updated|amended|approved|adopted)"
     r"(?:\s+as\s+of)?\s*:?\s*(?P<y>\d{4})",
     "year stated in the document"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), why) for p, why in _DATE_PATTERNS]


def _valid_year(y: int) -> bool:
    return _YEAR_MIN <= y <= _YEAR_MAX


def _build(year: int, month: int = 1, day: int = 1) -> Optional[datetime.date]:
    """Assemble a date, tolerating impossible combinations (Feb 30 -> None)."""
    try:
        return datetime.date(year, month or 1, day or 1)
    except ValueError:
        # A real day that does not exist in that month means we misread the text.
        # Fall back to the month rather than inventing a day.
        try:
            return datetime.date(year, month or 1, 1)
        except ValueError:
            return None


def date_from_text(text: str) -> Tuple[Optional[datetime.date], str]:
    """
    Find the document's own claim about when it takes effect.

    Returns `(date, why)` — `why` is shown to the admin so a wrong guess is
    obvious and correctable, rather than an unexplained number they have to trust.

    A school year "2025-2026" resolves to **June 2025**, not January: the
    Philippine academic year opens mid-year, so January would date the document
    five months before the year it describes and could lose a comparison against
    a document from the previous S.Y.
    """
    if not text or not isinstance(text, str):
        return None, ""

    # Only the first stretch of a document makes claims about itself; a date
    # deeper in the body is far more likely to be a deadline or an event.
    head = text[:4000]

    for rx, why in _COMPILED:
        m = rx.search(head)
        if not m:
            continue
        groups = m.groupdict()
        try:
            year = int(groups.get("y") or 0)
        except (TypeError, ValueError):
            continue
        if not _valid_year(year):
            continue

        month = _MONTHS.get((groups.get("m1") or "").lower(), 0)
        if not month and "school year" in why:
            # See the docstring: a Philippine S.Y. opens in June, and dating
            # "S.Y. 2025-2026" to January would place it five months before the
            # year it describes.
            month = 6


        day_raw = groups.get("d1") or groups.get("d2") or 0
        try:
            day = int(day_raw)
        except (TypeError, ValueError):
            day = 0
        if not 1 <= day <= 31:
            day = 1

        d = _build(year, month or 1, day)
        if d:
            return d, why

    return None, ""


def date_from_filename(name: str) -> Tuple[Optional[datetime.date], str]:
    """
    Last-resort signal: a 4-digit year in the filename.

    Weak, and labelled as such. `Samar-College-2024.pdf` is a real hint, but a
    file called `scan_20240103_002.pdf` is not making a claim about a school
    year — so a year is only accepted when it is delimited, not when it sits
    inside a longer run of digits.
    """
    base = os.path.basename((name or "").replace("\\", "/"))
    stem = os.path.splitext(base)[0]
    best: Optional[int] = None
    for m in re.finditer(r"(?<!\d)(\d{4})(?!\d)", stem):
        y = int(m.group(1))
        if _valid_year(y) and (best is None or y > best):
            best = y
    if best is None:
        return None, ""
    # June again, for the same academic-year reason as above.
    return datetime.date(best, 6, 1), "year found in the filename"


def infer_effective_date(
    filename: str,
    text: str = "",
    *,
    stated: str = "",
) -> dict:
    """
    Decide a document's effective date, most trustworthy source first.

    Never guesses "today" for an undated file — see the module docstring. The
    return value always explains itself:

        {"date": "2025-08-01", "source": "admin", "why": "...", "confidence": "high"}
        {"date": None,         "source": "",      "why": "", "confidence": "none"}
    """
    # 1. The admin's word, if given.
    if stated:
        d = _parse_iso_date(stated)
        if d:
            return {
                "date": d.isoformat(),
                "source": "admin",
                "why": "set by an administrator",
                "confidence": "high",
            }

    # 2. The document's own claim.
    d, why = date_from_text(text or "")
    if d:
        return {"date": d.isoformat(), "source": "document", "why": why,
                "confidence": "high"}

    # 3. The filename.
    d, why = date_from_filename(filename)
    if d:
        return {"date": d.isoformat(), "source": "filename", "why": why,
                "confidence": "low"}

    # 4. Unknown, and it says so.
    return {"date": None, "source": "", "why": "", "confidence": "none"}


def _parse_iso_date(s: str) -> Optional[datetime.date]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.date.fromisoformat(s.strip()[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Per-document metadata
# ---------------------------------------------------------------------------


def set_doc_date(
    filename: str,
    *,
    effective_date: str = "",
    text: str = "",
    set_by: str = "",
    note: str = "",
) -> dict:
    """
    Record (or correct) a document's effective date.

    Correcting overwrites in place rather than appending, so a fixed date takes
    effect immediately everywhere instead of leaving a stale row that a later
    comparison might still read.
    """
    key = doc_key(filename)
    if not key:
        raise ValueError("A filename is required.")

    if effective_date and not _parse_iso_date(effective_date):
        raise ValueError("Effective date must be YYYY-MM-DD.")

    inferred = infer_effective_date(filename, text, stated=effective_date)
    record = {
        "key": key,
        "filename": os.path.basename((filename or "").replace("\\", "/")),
        "effective_date": inferred["date"],
        "date_source": inferred["source"],
        "date_why": inferred["why"],
        "confidence": inferred["confidence"],
        "note": (note or "").strip()[:500],
        "set_by": set_by,
        "updated_at": _now(),
    }

    with _store_lock:
        store = _store().load()
        docs = store.setdefault("docs", {})
        existing = docs.get(key) or {}
        # First-seen is kept across corrections: it is the only record of when the
        # document entered the corpus, which is not the same as its own date.
        record["first_seen"] = existing.get("first_seen") or _now()
        docs[key] = record
        store["updated_at"] = _now()
        _store().save(store)

    return record


def get_doc_date(filename: str) -> Optional[dict]:
    return (_store().load().get("docs") or {}).get(doc_key(filename))


def list_docs() -> List[dict]:
    """
    Every known document, newest effective date first.

    Undated documents sort **last but are still listed** — they are the ones an
    admin most needs to see, because an undated file cannot win or lose a
    freshness comparison and will silently keep contradicting its neighbours.
    """
    docs = list((_store().load().get("docs") or {}).values())
    docs.sort(key=lambda d: (d.get("effective_date") or "", d.get("filename", "")),
              reverse=True)
    return docs


def delete_doc(filename: str) -> bool:
    key = doc_key(filename)
    with _store_lock:
        store = _store().load()
        docs = store.setdefault("docs", {})
        if key in docs:
            del docs[key]
            store["updated_at"] = _now()
            _store().save(store)
            return True
    return False


# ---------------------------------------------------------------------------
# Comparing two documents
# ---------------------------------------------------------------------------


def compare(a: str, b: str) -> dict:
    """
    Which of two documents is newer?

    Returns `{"newer": <filename or None>, "older": ..., "reason": "..."}`.

    `newer` is None when either date is unknown or the two are equal, and the
    reason says which. That is the whole point: an honest "cannot tell" sends the
    question to a human, whereas a guess produces an answer nobody can audit.
    """
    ma, mb = get_doc_date(a), get_doc_date(b)
    da = _parse_iso_date((ma or {}).get("effective_date") or "")
    db = _parse_iso_date((mb or {}).get("effective_date") or "")

    if da and db:
        if da > db:
            return {"newer": a, "older": b,
                    "reason": f"{os.path.basename(a)} is dated {da.isoformat()}, "
                              f"{os.path.basename(b)} is dated {db.isoformat()}"}
        if db > da:
            return {"newer": b, "older": a,
                    "reason": f"{os.path.basename(b)} is dated {db.isoformat()}, "
                              f"{os.path.basename(a)} is dated {da.isoformat()}"}
        return {"newer": None, "older": None,
                "reason": f"both documents are dated {da.isoformat()}"}

    missing = [os.path.basename(n) for n, m in ((a, da), (b, db)) if not m]
    return {"newer": None, "older": None,
            "reason": "no effective date on record for " + ", ".join(missing)}


# ---------------------------------------------------------------------------
# The pre-index scan
# ---------------------------------------------------------------------------


def _facts_by_key(facts: Iterable[Fact]) -> Dict[str, List[Fact]]:
    out: Dict[str, List[Fact]] = {}
    for f in facts:
        if f.role and f.subject and f.person:
            out.setdefault(f.key, []).append(f)
    return out


def preview_upload(
    filename: str,
    pages: Iterable[Tuple[str, object]],
    existing_facts: Iterable[Fact] = (),
    *,
    stated_date: str = "",
) -> dict:
    """
    Scan an incoming document BEFORE it is indexed.

    `pages` is an iterable of `(text, page_number)`. `existing_facts` are the
    facts already in the corpus — the caller supplies them because this module
    must not reach into Pinecone (that would make it untestable and slow, and it
    would couple a data-quality check to a vector store being reachable).

    Returns what the admin needs to make one decision per contradiction:

        incoming_date : what we think this file's effective date is, and why
        conflicts     : [{key, label, incoming, existing, newer, recommendation}]
        agreements    : facts the new file confirms — evidence the upload is sane
        new_facts     : facts nobody had before

    `recommendation` is phrased as advice, never applied automatically. See the
    module docstring: a newer file can still be a draft.
    """
    text_all: List[str] = []
    incoming: List[Fact] = []
    for text, page in pages:
        text_all.append(text or "")
        incoming.extend(extract_facts(text or "", source=filename, page=page))

    joined = "\n".join(text_all)
    date_info = infer_effective_date(filename, joined, stated=stated_date)

    mine = _facts_by_key(incoming)
    theirs = _facts_by_key(existing_facts)

    conflicts: List[dict] = []
    agreements: List[dict] = []
    new_facts: List[dict] = []

    for key, my_facts in sorted(mine.items()):
        my_people = sorted({f.person for f in my_facts})
        other = theirs.get(key)

        if not other:
            new_facts.append({
                "key": key,
                "label": _label(key),
                "value": display_name(_first_evidence_name(my_facts)),
                "pages": sorted({f.page for f in my_facts if f.page is not None}),
            })
            continue

        their_people = sorted({f.person for f in other})
        if set(my_people) == set(their_people):
            agreements.append({
                "key": key,
                "label": _label(key),
                "value": display_name(_first_evidence_name(my_facts)),
            })
            continue

        # A real contradiction: same role+subject, different person.
        their_sources = sorted({f.source for f in other if f.source})
        newer = None
        reason = ""
        if their_sources:
            cmp = compare(filename, their_sources[0])
            newer, reason = cmp["newer"], cmp["reason"]

        conflicts.append({
            "key": key,
            "label": _label(key),
            "incoming": {
                "value": display_name(_first_evidence_name(my_facts)),
                "source": os.path.basename(filename),
                "pages": sorted({f.page for f in my_facts if f.page is not None}),
                "effective_date": date_info["date"],
            },
            "existing": {
                "value": display_name(_first_evidence_name(other)),
                "sources": [os.path.basename(s) for s in their_sources],
                "pages": sorted({f.page for f in other if f.page is not None}),
            },
            "newer": os.path.basename(newer) if newer else None,
            "reason": reason,
            "recommendation": _recommend(filename, newer, date_info),
        })

    return {
        "filename": os.path.basename((filename or "").replace("\\", "/")),
        "incoming_date": date_info,
        "conflicts": conflicts,
        "agreements": agreements,
        "new_facts": new_facts,
        "summary": {
            "conflicts": len(conflicts),
            "agreements": len(agreements),
            "new_facts": len(new_facts),
            "facts_found": len(mine),
        },
    }


def _label(key: str) -> str:
    role, _, subject = key.partition("|")
    return f"{role.title()} of {' '.join(w.capitalize() for w in subject.split())}"


def _first_evidence_name(facts: List[Fact]) -> str:
    """
    The most-repeated person among these facts.

    Most-repeated rather than first-seen: a name stated three times on a "DEANS
    UPDATE" page is a stronger reading of the document than one that appears once
    in a signature block, and the admin should be shown the dominant claim.
    """
    counts: Dict[str, int] = {}
    for f in facts:
        counts[f.person] = counts.get(f.person, 0) + 1
    best = max(counts, key=lambda p: counts[p]) if counts else ""
    for f in facts:
        if f.person == best:
            ev = f.evidence or ""
            head = re.split(r"\s+[-–—/]\s+", ev)[0] if ev else ""
            if head and normalize_person(head) == best:
                return head
            break
    return best.title()


def _recommend(filename: str, newer: Optional[str], date_info: dict) -> str:
    """
    Advice for the admin, worded so that acting on it is a choice.

    When freshness cannot decide, this says so plainly instead of falling back to
    "use the new one" — the failure mode that would make an uploaded 2019 scan
    quietly overwrite the current handbook.
    """
    if newer and doc_key(newer) == doc_key(filename):
        return ("The document you are uploading is newer, so its value is most "
                "likely current — confirm and pin it.")
    if newer:
        return (f"The existing {os.path.basename(newer)} is newer than this file, "
                "so the value already indexed is probably the current one.")
    if date_info.get("confidence") == "none":
        return ("This file has no effective date, so freshness cannot break the "
                "tie. Set a date, or pin the correct value directly.")
    return ("Effective dates do not settle this. Pin whichever value you know to "
            "be current.")


# ---------------------------------------------------------------------------
# Prompt surface
# ---------------------------------------------------------------------------


def freshness_block(*sources: str) -> str:
    """
    Tell the model the relative age of the documents it is about to quote.

    Only emitted when at least two of the cited sources have *different* known
    dates, because that is the only situation where it changes an answer. A block
    listing one document's date is pure token cost, and one listing two identical
    dates invites the model to invent a distinction that does not exist.
    """
    seen: Dict[str, dict] = {}
    for s in sources:
        if not s:
            continue
        meta = get_doc_date(s)
        if meta and meta.get("effective_date"):
            seen.setdefault(doc_key(s), meta)

    dates = {m["effective_date"] for m in seen.values()}
    if len(seen) < 2 or len(dates) < 2:
        return ""

    ordered = sorted(seen.values(), key=lambda m: m["effective_date"], reverse=True)
    lines = [
        "<document_dates>",
        "The sources below are dated. When two sources disagree on a fact, prefer "
        "the one with the LATER effective date and say which document you followed.",
    ]
    for i, m in enumerate(ordered):
        tag = "NEWEST" if i == 0 else ("OLDEST" if i == len(ordered) - 1 else "older")
        lines.append(f"- {m['filename']} — effective {m['effective_date']} ({tag})")
    lines.append("</document_dates>")
    return "\n".join(lines)
