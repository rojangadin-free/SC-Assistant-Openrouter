"""
rag/conflicts.py — detect CONTRADICTORY facts across (and inside) documents, and
let an admin pin the correct value so the assistant stops guessing.

The bug this solves
-------------------
`data/Samar-College-update.pdf` states the Dean of the College of Education
twice, with two different people:

    p15  "DEANS UPDATE ... Jacqueline Montalis - College of Education"
    p26  "Dr. Nimfa T. Torremoro / Dean, College of Graduate Studies & College of Education"

Both sentences are indexed, both are retrieved for "who is the dean of CoEd?",
and both are equally "grounded". The LLM has no way to know which is current, so
the answer flips between runs. No amount of prompt engineering fixes a corpus
that contradicts itself — the contradiction has to be *detected* and *resolved*
by a human once, then enforced at answer time.

Three pieces, all document-agnostic
-----------------------------------
1. `extract_facts()`   — pull `(role, subject) -> person` assertions out of raw
                         page text using shape-based patterns (no vocabulary
                         about Samar College anywhere).
2. `find_conflicts()`  — group facts by their normalized key and report any key
                         with more than one distinct person.
3. `RESOLUTIONS`       — admin decisions, persisted as JSON. `authority_block()`
                         renders them into the prompt as the single source of
                         truth, and `explain_for_admin()` powers the review UI.

Why key/value extraction instead of "semantic similarity of chunks"
-------------------------------------------------------------------
Near-duplicate detection (embeddings/minhash) finds text that *looks* the same.
The failure here is the opposite: two short, lexically different lines that
assign the same ROLE to different PEOPLE. Only a key/value view makes that
visible, and it also gives the admin something concrete to choose between:

    role   : dean
    subject: college of education
    values : "Jacqueline Montalis"  (update.pdf p15)
             "Nimfa T. Torremoro"   (update.pdf p26, 2024.pdf p146)

The same machinery generalises to any "TITLE, ORG = NAME" statement — principal,
director, registrar, program head — because the patterns are structural.
"""

from __future__ import annotations

import os
import re
import threading
import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from rag.store import JsonBlobStore


# ---------------------------------------------------------------------------
# Where admin decisions live.
#
# Delegated to `rag.store`, which keeps the same JSON file by default and can be
# pointed at DynamoDB with STORE_BACKEND=dynamodb. That switch matters here more
# than anywhere else in the project: a resolution is *enforcement*, so if one
# container cannot see the decision another container recorded, the assistant
# keeps answering with the name an admin already ruled out — silently, and only
# for some students.
#
# The path stays env-overridable because every test redirects it to a temp file.
# ---------------------------------------------------------------------------
RESOLUTIONS_FILE = os.getenv("CONFLICT_RESOLUTIONS_FILE", "conflict_resolutions.json")


def _empty_store() -> dict:
    return {"resolutions": {}, "updated_at": ""}


# Built lazily so that changing CONFLICT_RESOLUTIONS_FILE after import (which is
# exactly what the test suites do) still takes effect.
_store_obj: Optional[JsonBlobStore] = None
_store_obj_path: Optional[str] = None
_store_lock = threading.Lock()


def _store():
    global _store_obj, _store_obj_path
    path = os.getenv("CONFLICT_RESOLUTIONS_FILE", RESOLUTIONS_FILE)
    if _store_obj is None or _store_obj_path != path:
        _store_obj = JsonBlobStore("conflicts", path, _empty_store)
        _store_obj_path = path
    return _store_obj



# ============================================================
# NORMALIZATION
# ============================================================

# Honorifics and post-nominals: "Dr. Nimfa T. Torremoro, PhD (SGD)" and
# "NIMFA T. TORREMORO" must collapse to the same person, otherwise every
# document formatting difference looks like a conflict.
_TITLES = re.compile(
    r"^(dr|dra|mr|mrs|ms|miss|engr|atty|prof|professor|rev|hon|sr|jr)\.?\s+",
    re.I,
)
_POSTNOMINAL = re.compile(
    r"[,\s]+(ph\.?d|ed\.?d|d\.?p\.?a|m\.?a\.?ed|m\.?a|m\.?s|mba|cpa|rn|llb|jd|sgd)\b\.?",
    re.I,
)
_PARENTHETICAL = re.compile(r"\([^)]*\)")

# Words that carry no identity ("the", "office of") when comparing subjects.
_SUBJECT_STOPWORDS = {"the", "of", "for", "and", "&", "in", "at", "a", "an"}

# Role synonyms: the *same* job written differently must produce the same key,
# or a conflict silently splits into two harmless-looking single-value keys.
_ROLE_CANON = {
    "dean": "dean",
    "deans": "dean",
    "dean's": "dean",
    "acting dean": "dean",
    "college dean": "dean",
    "principal": "principal",
    "school principal": "principal",
    "director": "director",
    "president": "president",
    "school president": "president",
    "vice president": "vice president",
    "vp": "vice president",
    "svp": "senior vice president",
    "senior vice president": "senior vice president",
    "registrar": "registrar",
    "school registrar": "registrar",
    "program head": "program head",
    "program chair": "program head",
    "head": "head",
    "chair": "chair",
    "chairman": "chair",
    "coordinator": "coordinator",
    "adviser": "adviser",
    "advisor": "adviser",
}

# The roles we bother extracting. Anything else is prose, not a directory entry.
_ROLE_WORDS = sorted(_ROLE_CANON.keys(), key=len, reverse=True)
_ROLE_ALT = "|".join(re.escape(r) for r in _ROLE_WORDS)


def normalize_person(name: str) -> str:
    """
    Canonical form of a person's name for comparison.

    "Dr. Nimfa T. Torremoro"  -> "nimfa torremoro"
    "NIMFA T. TORREMORO, PhD" -> "nimfa torremoro"
    "Torremoro, Nimfa T."     -> "nimfa torremoro"   (comma form is flipped)

    Middle initials are dropped because documents include them inconsistently;
    keeping them would report "Nimfa T. Torremoro" vs "Nimfa Torremoro" as a
    conflict, which is noise an admin would (rightly) stop trusting.
    """
    s = (name or "").strip()
    if not s:
        return ""

    s = _PARENTHETICAL.sub(" ", s)
    s = _POSTNOMINAL.sub(" ", s)
    s = _TITLES.sub("", s.strip())
    s = s.replace(".", " ").replace("–", " ").replace("—", " ")

    # "Surname, Firstname" -> "Firstname Surname"
    if s.count(",") == 1:
        left, right = [p.strip() for p in s.split(",")]
        if left and right and len(right.split()) <= 3:
            s = f"{right} {left}"

    # Hyphens/apostrophes inside names are pure formatting noise:
    # "Mary-Ann D. Abaigar" and "Mary Ann Abaigar" are one person, and treating
    # them as two would flood the admin queue with fake conflicts.
    s = s.replace("-", " ").replace("'", " ").replace("’", " ")
    s = re.sub(r"[^A-Za-z ]", " ", s)
    parts = [p for p in s.split() if p]
    # Drop single-letter middle initials.
    parts = [p for p in parts if len(p) > 1]
    return " ".join(parts).lower()


def normalize_subject(subject: str) -> str:
    """
    Canonical form of the ORG a role belongs to.

    "College of Education"                  -> "college education"
    "the College of Education (CoEd)"       -> "college education coed"
    "COLLEGE OF EDUCATION"                  -> "college education"
    """
    s = (subject or "").strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    words = [w for w in s.split() if w and w not in _SUBJECT_STOPWORDS]
    return " ".join(words)


def canonical_role(role: str) -> str:
    r = re.sub(r"\s+", " ", (role or "").strip().lower())
    r = r.strip(".,:;")
    return _ROLE_CANON.get(r, r)


def display_name(name: str) -> str:
    """Human-friendly form kept for the UI (the raw text, lightly tidied)."""
    s = re.sub(r"\s+", " ", (name or "").strip())
    return s.strip(" ,–—-")


# ============================================================
# FACT EXTRACTION
# ============================================================

# Shape 1 — "NAME – College of Education" / "NAME - Office of Student Affairs"
#           (the DEANS UPDATE list on update.pdf p15). The role comes from the
#           section heading, so it is supplied by the caller.
_ROSTER_LINE = re.compile(
    r"^\s*(?:(?:dr|dra|mr|mrs|ms|miss|engr|atty|prof|rev|hon)\.?\s+)?"
    r"(?P<person>[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']*){1,4})"
    r"\s*[-–—]\s*"
    r"(?P<subject>[A-Za-z][A-Za-z0-9 .,&'/()-]{3,80})\s*$"
)

# Shape 2 — a name line followed by a "ROLE, SUBJECT" line (the signature blocks
#           on update.pdf pp.16-31, and ANNEX C in the 2024 handbook).
_ROLE_SUBJECT_LINE = re.compile(
    rf"^\s*(?P<role>{_ROLE_ALT})\s*[,:]\s*(?P<subject>[A-Za-z][A-Za-z0-9 .,&'/()-]{{2,80}})\s*$",
    re.I,
)

# Shape 3 — "ROLE of SUBJECT is NAME" / "NAME is the ROLE of SUBJECT" in prose.
_PROSE_ROLE_IS_NAME = re.compile(
    rf"\b(?P<role>{_ROLE_ALT})\s+(?:of|for)\s+(?:the\s+)?"
    rf"(?P<subject>[A-Z][A-Za-z0-9 &'/-]{{3,60}}?)\s+is\s+"
    rf"(?:(?:dr|dra|mr|mrs|ms|miss|engr|atty|prof)\.?\s+)?"
    rf"(?P<person>[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']*){{1,3}})",
    re.I,
)

_PROSE_NAME_IS_ROLE = re.compile(
    rf"(?:(?:dr|dra|mr|mrs|ms|miss|engr|atty|prof)\.?\s+)?"
    rf"(?P<person>[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']*){{1,3}})"
    rf"\s+(?:is|was)\s+(?:the\s+|assigned\s+as\s+the\s+|appointed\s+as\s+the\s+)?"
    rf"(?P<role>{_ROLE_ALT})\s+(?:of|for)\s+(?:the\s+)?"
    rf"(?P<subject>[A-Z][A-Za-z0-9 &'/-]{{3,60}})",
)

# A line that looks like a person's name on its own (signature blocks).
_NAME_ONLY = re.compile(
    r"^\s*(?:(?P<title>dr|dra|mr|mrs|ms|miss|engr|atty|prof|rev|hon)\.?\s+)?"
    r"(?P<person>[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']*){1,4})"
    r"(?:\s*,\s*(?:Ph\.?D|Ed\.?D|D\.?P\.?A|MAEd|MA|MS|MBA|CPA|RN)\.?)?"
    r"\s*(?:\(SGD\))?\s*$",
    re.I,
)

# Section heading that turns a bare roster line into a role ("DEANS UPDATE" ->
# these are deans). Structural: <role word> + optional noise, nothing else.
_ROLE_HEADING = re.compile(rf"^\W*(?P<role>{_ROLE_ALT})S?\b[\s\W]*(update|list|directory)?\W*$", re.I)

# Lines that are never people (URLs, page furniture, sentences).
_NOT_A_PERSON = re.compile(r"(https?://|@|\d{3,}|:\s|\.$|,\s*(and|the)\b)", re.I)

# Subjects that are placeholders, not real assignments.
_PLACEHOLDER_PERSON = re.compile(r"^(tbd|tba|vacant|n/?a|none|to be (determined|announced))$", re.I)


class Fact:
    """One `(role, subject) = person` assertion, with where it came from."""

    __slots__ = ("role", "subject", "person", "source", "page", "evidence")

    def __init__(self, role: str, subject: str, person: str,
                 source: str = "", page: Optional[int] = None, evidence: str = ""):
        self.role = canonical_role(role)
        self.subject = normalize_subject(subject)
        self.person = normalize_person(person)
        self.source = source
        self.page = page
        self.evidence = re.sub(r"\s+", " ", (evidence or "")).strip()[:300]

    @property
    def key(self) -> str:
        return f"{self.role}|{self.subject}"

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "subject": self.subject,
            "person": self.person,
            "person_display": display_name(self.evidence_person or self.person),
            "source": self.source,
            "page": self.page,
            "evidence": self.evidence,
        }

    # The raw (un-normalized) name is worth keeping for display; it is derived
    # from the evidence line, so recompute rather than store another field.
    @property
    def evidence_person(self) -> str:
        return ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Fact {self.role} of {self.subject!r} = {self.person!r} @{self.source} p{self.page}>"


def _is_plausible_person(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    if _NOT_A_PERSON.search(t):
        return False
    if _PLACEHOLDER_PERSON.match(t.strip(" .")):
        return False
    norm = normalize_person(t)
    # A real name has at least a first and last name after normalization.
    return len(norm.split()) >= 2


def extract_facts(
    text: str,
    source: str = "",
    page: Optional[int] = None,
) -> List[Fact]:
    """
    Pull role assertions out of one page/chunk of text.

    Deliberately conservative: a missed fact costs nothing (the conflict simply
    is not reported), while a bogus fact wastes an admin's attention. Everything
    matched here has an explicit ROLE word in it.
    """
    facts: List[Fact] = []
    if not text:
        return facts

    raw_lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in raw_lines if ln]

    # The role implied by the most recent role-ish heading ("DEANS UPDATE").
    heading_role = ""

    for i, line in enumerate(lines):
        m_head = _ROLE_HEADING.match(line)
        if m_head and len(line) <= 40:
            heading_role = canonical_role(m_head.group("role"))
            continue

        # --- Shape 1: "NAME - SUBJECT" under a role heading -----------------
        m = _ROSTER_LINE.match(line)
        if m and heading_role:
            person, subject = m.group("person"), m.group("subject")
            if _is_plausible_person(person) and normalize_subject(subject):
                facts.append(Fact(heading_role, subject, person, source, page, line))
                continue

        # --- Shape 2: NAME line then "ROLE, SUBJECT" line -------------------
        m = _ROLE_SUBJECT_LINE.match(line)
        if m:
            role, subject = m.group("role"), m.group("subject")
            # The name is normally the line just above; allow one blank-ish gap.
            for back in (1, 2):
                if i - back < 0:
                    break
                cand = lines[i - back]
                mn = _NAME_ONLY.match(cand)
                if mn and _is_plausible_person(mn.group("person")):
                    # A subject can name several orgs at once:
                    # "Dean, College of Graduate Studies & College of Education"
                    for part in _split_subjects(subject):
                        facts.append(
                            Fact(role, part, mn.group("person"), source, page,
                                 f"{cand} / {line}")
                        )
                    break
            continue

        # --- Shape 3: prose ------------------------------------------------
        for pm in _PROSE_ROLE_IS_NAME.finditer(line):
            if _is_plausible_person(pm.group("person")):
                facts.append(
                    Fact(pm.group("role"), pm.group("subject"), pm.group("person"),
                         source, page, line)
                )
        for pm in _PROSE_NAME_IS_ROLE.finditer(line):
            if _is_plausible_person(pm.group("person")):
                facts.append(
                    Fact(pm.group("role"), pm.group("subject"), pm.group("person"),
                         source, page, line)
                )

    return facts


def _split_subjects(subject: str) -> List[str]:
    """
    "College of Graduate Studies & College of Education"
        -> ["College of Graduate Studies", "College of Education"]

    One person can hold the same role in two units, and that is NOT a conflict;
    but it must be recorded against BOTH units, otherwise the real conflict on
    one of them stays invisible.
    """
    parts = re.split(r"\s*(?:&|/| and )\s*(?=college|office|school|department|institute)",
                     subject, flags=re.I)
    out = [p.strip(" ,;") for p in parts if p and p.strip(" ,;")]
    return out or [subject]


# ============================================================
# CONFLICT DETECTION
# ============================================================

class Conflict:
    """
    One `(role, subject)` key that documents disagree about.

    `values` maps normalized person -> list of occurrences, so the UI can show
    "who says what, and where" and the admin can pick a winner.
    """

    def __init__(self, key: str, role: str, subject: str):
        self.key = key
        self.role = role
        self.subject = subject
        self.values: Dict[str, List[dict]] = {}

    def add(self, fact: Fact, raw_person: str):
        self.values.setdefault(fact.person, []).append({
            "person_display": display_name(raw_person),
            "source": fact.source,
            "page": fact.page,
            "evidence": fact.evidence,
        })

    @property
    def subject_display(self) -> str:
        return " ".join(w.capitalize() for w in self.subject.split())

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "role": self.role,
            "subject": self.subject,
            "subject_display": self.subject_display,
            "label": f"{self.role.title()} of {self.subject_display}",
            "candidates": [
                {
                    "person": person,
                    "person_display": occs[0]["person_display"] or person.title(),
                    "occurrences": occs,
                    "count": len(occs),
                    "sources": sorted({o["source"] for o in occs if o["source"]}),
                }
                for person, occs in sorted(
                    self.values.items(), key=lambda kv: -len(kv[1])
                )
            ],
        }


def _raw_person_of(fact: Fact) -> str:
    """
    Best-effort human-readable name for a fact, recovered from its evidence.
    Kept separate from Fact so Fact stays a small, comparable value object.
    """
    ev = fact.evidence or ""
    # Shape 1 evidence is "Name - Subject"; shape 2 is "Name / Role, Subject".
    head = re.split(r"\s+[-–—/]\s+", ev)[0] if ev else ""
    if head and normalize_person(head) == fact.person:
        return head
    return fact.person.title()


def find_conflicts(facts: Iterable[Fact], min_values: int = 2) -> List[Conflict]:
    """
    Group facts by `(role, subject)` and return the keys with more than one
    distinct person. Ordered by "most contested" first.
    """
    buckets: Dict[str, Conflict] = {}
    for f in facts:
        if not f.role or not f.subject or not f.person:
            continue
        c = buckets.setdefault(f.key, Conflict(f.key, f.role, f.subject))
        c.add(f, _raw_person_of(f))

    conflicts = [c for c in buckets.values() if len(c.values) >= min_values]
    conflicts.sort(key=lambda c: (-len(c.values), c.key))
    return conflicts


def scan_documents(docs: Iterable) -> Tuple[List[Fact], List[Conflict]]:
    """
    Convenience wrapper for a list of LangChain Documents (or anything with
    `.page_content` / `.metadata`).
    """
    facts: List[Fact] = []
    for d in docs:
        md = getattr(d, "metadata", None) or {}
        facts.extend(
            extract_facts(
                getattr(d, "page_content", "") or "",
                source=md.get("source", ""),
                page=md.get("page"),
            )
        )
    return facts, find_conflicts(facts)


# ============================================================
# ADMIN RESOLUTIONS (the "which one is correct?" decisions)
# ============================================================

# These two are thin now, but kept as named functions: they are the seam the
# module docstring promises, and `check_data_conflicts.py` calls them directly.
def _load_store() -> dict:
    return _store().load()


def _save_store(store: dict) -> None:
    _store().save(store)


def list_resolutions() -> Dict[str, dict]:
    return _load_store().get("resolutions", {})



def set_resolution(
    key: str,
    person_display: str,
    *,
    role: str = "",
    subject: str = "",
    note: str = "",
    resolved_by: str = "",
    rejected: Optional[List[str]] = None,
) -> dict:
    """
    Pin the correct value for one `(role, subject)` key.

    `rejected` records the values the admin ruled out. That is what makes the
    fix airtight at answer time: the prompt can say "X is correct, and the
    document also contains Y — ignore Y", so a retrieved chunk carrying the
    wrong name cannot be repeated as if it were current.
    """
    entry = {
        "key": key,
        "role": canonical_role(role) or (key.split("|")[0] if "|" in key else ""),
        "subject": normalize_subject(subject) or (key.split("|")[1] if "|" in key else ""),
        "correct": display_name(person_display),
        "correct_normalized": normalize_person(person_display),
        "rejected": [display_name(r) for r in (rejected or []) if display_name(r)],
        "note": (note or "").strip()[:500],
        "resolved_by": resolved_by,
        "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with _store_lock:
        store = _load_store()
        store.setdefault("resolutions", {})[key] = entry
        _save_store(store)
    return entry


def delete_resolution(key: str) -> bool:
    with _store_lock:
        store = _load_store()
        if key in store.get("resolutions", {}):
            del store["resolutions"][key]
            _save_store(store)
            return True
    return False


# ============================================================
# ANSWER-TIME ENFORCEMENT
# ============================================================

def _key_terms(key: str) -> List[str]:
    role, _, subject = key.partition("|")
    return [t for t in (role.split() + subject.split()) if len(t) > 2]


def relevant_resolutions(*texts: str) -> List[dict]:
    """
    Return the resolutions that apply to the current question/context.

    Matching is by subject terms appearing in the text, so a question about the
    College of Education pulls in the CoEd dean decision and nothing else. This
    keeps the injected block small — an authority list that scrolls for a page
    would dilute the instruction it is meant to enforce.
    """
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return []

    out = []
    for key, entry in list_resolutions().items():
        subject_terms = [t for t in entry.get("subject", "").split() if len(t) > 2]
        role = entry.get("role", "")
        if not subject_terms:
            continue
        # Require the role word AND at least half of the subject's terms, so
        # "college" alone cannot drag in every college's dean.
        hits = sum(1 for t in subject_terms if t in blob)
        if role and role.split()[0] in blob and hits >= max(1, len(subject_terms) // 2):
            out.append(entry)
        # Also match on a rejected/correct name appearing verbatim: if a wrong
        # name is in the retrieved context, the correction must come with it.
        elif any(
            n and n.lower() in blob
            for n in [entry.get("correct", "")] + entry.get("rejected", [])
        ):
            out.append(entry)
    return out


def authority_block(*texts: str) -> str:
    """
    Render the applicable resolutions as a prompt section.

    Empty string when nothing applies, so the prompt is unchanged for the vast
    majority of questions.
    """
    entries = relevant_resolutions(*texts)
    if not entries:
        return ""

    lines = [
        "<verified_facts>",
        "These values were reviewed and confirmed by a college administrator.",
        "They OVERRIDE any conflicting statement in the retrieved documents.",
        "If a retrieved document states a different value for one of these, the "
        "document is out of date: use the verified value and do not mention the "
        "outdated one.",
        "",
    ]
    for e in entries:
        label = f"{e.get('role', '').title()} of {' '.join(w.capitalize() for w in e.get('subject', '').split())}"
        lines.append(f"- {label}: {e.get('correct', '')}")
        if e.get("rejected"):
            lines.append(f"  (outdated / incorrect, do NOT use: {', '.join(e['rejected'])})")
        if e.get("note"):
            lines.append(f"  note: {e['note']}")
    lines.append("</verified_facts>")
    return "\n".join(lines)


def explain_for_admin(conflicts: List[Conflict]) -> List[dict]:
    """
    Serialize conflicts for the admin UI, merged with any existing decision so
    the page can show "resolved / unresolved" without a second request.
    """
    resolved = list_resolutions()
    out = []
    for c in conflicts:
        d = c.as_dict()
        d["resolution"] = resolved.get(c.key)
        d["status"] = "resolved" if c.key in resolved else "unresolved"
        out.append(d)
    # Unresolved first — that is the admin's work queue.
    out.sort(key=lambda d: (d["status"] == "resolved", d["label"]))
    return out
