"""
rag/gaps.py — record the questions the assistant could NOT answer, so the
documents can be fixed instead of guessed at.

Why this exists
---------------
The prompt in `src/prompt.py` forces strict grounding: when the retrieved pages
don't contain the answer, the assistant must say so. That is the right
behaviour, but as built the information dies there — the student leaves with
nothing and nobody learns that a topic is missing from the corpus.

This module turns every one of those dead ends into a data point:

    student asks something the documents don't cover
        -> the reply is detected as a "no-answer"
        -> the question is logged, grouped with semantically similar ones
        -> the admin sees a ranked list: "34 students asked about X, we have
           no document that answers it"

That ranked list IS the document roadmap, generated from real usage rather than
from someone's assumption about what students want to know.

Design notes
------------
* **Detection is text-based, not model-based.** A second LLM call per turn to
  ask "did you answer?" would double cost and latency on every message. The
  refusal wording is dictated by our own system prompt, so matching that wording
  is both cheap and accurate. `looks_unanswered()` is deliberately conservative:
  it only fires on short replies that contain a refusal phrase and carry no
  citation, because a long answer with sources is an answer even if it hedges
  somewhere in the middle.

* **Grouping is by normalized content words.** "how do i enroll", "How to
  enroll?" and "enrollment process po" must land on ONE row, otherwise the admin
  sees 40 near-identical lines and can't tell what actually matters. Stopwords
  and Filipino particles are stripped, the remaining words are sorted, and the
  result is the group key — no embeddings needed, no extra dependency, works
  offline.

* **Storage mirrors rag/conflicts.py** (JSON + lock, swappable for DynamoDB) so
  there is one storage story in this codebase, not two.
"""

from __future__ import annotations

import os
import re
import threading
import datetime
from typing import Dict, List, Optional

from rag.store import JsonBlobStore

GAPS_FILE = os.getenv("CONTENT_GAPS_FILE", "content_gaps.json")


_lock = threading.Lock()

# Keep the log bounded: this is a prioritisation tool, not an audit trail. The
# examples are what a human reads, and five phrasings is already more than
# enough to understand what students mean.
MAX_EXAMPLES_PER_GAP = 5
MAX_GAPS = 500


# ============================================================
# DETECTION
# ============================================================

# The phrases our own system prompt tells the model to use when the documents
# fall short. Matching our instructions (rather than guessing at arbitrary LLM
# phrasing) is what makes this reliable.
_REFUSAL_PATTERNS = [
    r"\bi (?:do not|don't) (?:have|know)\b",
    r"\bnot (?:provided|available|included|specified|mentioned|found)\b",
    r"\b(?:no|not any) (?:information|details|record|data)\b",
    r"\bcould not find\b",
    r"\bcouldn't find\b",
    r"\bunable to (?:find|locate|answer|provide)\b",
    r"\bis not covered\b",
    r"\bmy (?:current )?documents do not\b",
    r"\bdocuments (?:do not|don't) (?:contain|include|mention|specify)\b",
    r"\bwala (?:po )?(?:akong|sa) (?:impormasyon|datos)\b",   # Filipino
]
_REFUSAL = re.compile("|".join(_REFUSAL_PATTERNS), re.I)

# A citation badge means the answer was grounded in a real page.
_HAS_CITATION = re.compile(r"\[SOURCE:", re.I)

# Refusals are short. A long reply that happens to contain "not specified" in
# one clause is a real answer with a caveat, and logging it would fill the
# admin's queue with noise.
_MAX_REFUSAL_CHARS = 400

# A refusal is essentially the whole message. If the refusal phrase sits deep
# inside a substantial answer, the answer is the point and the caveat is an
# aside — measured as a fraction so it holds for any message length.
_REFUSAL_POSITION_RATIO = 0.5


def looks_unanswered(answer: str) -> bool:
    """
    True when `answer` is the assistant admitting it has no grounded answer.

    Conservative on purpose: a false positive costs the admin attention, and a
    queue full of noise is a queue nobody reads. Three guards, all cheap:

      1. a grounded citation means it answered;
      2. real answers are longer than refusals;
      3. in a genuine refusal the phrase appears up front, not buried after
         several sentences of actual content.
    """
    if not answer:
        return False
    text = answer.strip()
    if _HAS_CITATION.search(text):
        return False

    match = _REFUSAL.search(text)
    if not match:
        return False

    if len(text) > _MAX_REFUSAL_CHARS:
        return False

    # "The fee is X, though the graduate table is not specified" -> answered.
    if match.start() > len(text) * _REFUSAL_POSITION_RATIO:
        return False

    return True



# ============================================================
# GROUPING
# ============================================================

# English stopwords + the Filipino particles and pronouns that dominate real
# student phrasing ("paano po ba mag-enroll"). Dropping them is what makes
# "how do I enroll" and "paano mag enroll po" collapse to the same key.
_STOPWORDS = {
    # English
    "a", "about", "am", "an", "and", "any", "are", "as", "at", "be", "been",
    "can", "could", "did", "do", "does", "for", "from", "get", "give", "has",
    "have", "how", "i", "if", "in", "into", "is", "it", "its", "know", "like",
    "me", "much", "must", "my", "need", "of", "on", "or", "please", "should",
    "so", "some", "tell", "than", "that", "the", "their", "them", "there",
    "these", "they", "this", "to", "us", "want", "was", "we", "were", "what",
    "when", "where", "which", "who", "whom", "why", "will", "with", "would",
    "you", "your",
    # Filipino / Taglish
    "ako", "akin", "ang", "ano", "ba", "bakit", "dito", "ganito", "ho", "ika",
    "ilan", "kailan", "kami", "kayo", "ko", "kung", "lang", "mag", "may",
    "maaari", "mga", "na", "naman", "nasaan", "ng", "nga", "nila", "nino",
    "niya", "o", "pa", "paano", "pala", "para", "po", "pwede", "sa", "saan",
    "sana", "si", "sila", "siya", "tayo", "yung",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-']*")

# Filipino verb affixes, only stripped when a hyphen makes the intent explicit
# ("mag-enroll") or when the remainder is still a substantial word. Bare "ma"/
# "in"/"um" would maul ordinary English words ("machine" -> "chine"), so the
# safer prefixes are matched and the aggressive ones require the hyphen.
_TAGALOG_PREFIX_HYPHEN = re.compile(r"^(?:mag|nag|pag|ipag|makapag|maka|ma|um|in)-", re.I)
_TAGALOG_PREFIX_BARE = re.compile(r"^(?:magpa|makapag|ipag|mag|nag|pag)(?=[a-z]{5,})", re.I)

# Crude English suffix folding so "enroll", "enrolling" and "enrollment" group
# together. A real stemmer (nltk/snowball) would be more precise, but that is a
# heavy dependency for a grouping heuristic that only needs to be good enough
# for a human-reviewed queue.
#
# Order matters: longest first, so "enrollment" loses "ment" and not "s".
_SUFFIXES = (
    "ements", "ement", "ments", "ment",
    "ations", "ation", "tions", "tion",
    "ings", "ing", "ies", "ers", "er", "es", "s",
)

# Never fold below this length: "fees" -> "fee" is fine, but stripping further
# produces collisions between unrelated words.
_MIN_STEM = 4

# Words whose "suffix" is part of the root. Stripping these produces nonsense
# stems ("instructions" -> "struction", "service" -> "servic") that then fail to
# match the un-suffixed form, which is exactly the bug this guard prevents.
_STEM_OVERRIDES = {
    "instruction": "instruct",
    "instructions": "instruct",
    "instructional": "instruct",
    "service": "serv",
    "services": "serv",
    "process": "process",
    "processes": "process",
    "class": "class",
    "classes": "class",
    "fees": "fee",
    "fee": "fee",
    "requirements": "requir",
    "requirement": "requir",
    "require": "requir",
    "required": "requir",
    "enrollment": "enroll",
    "enrolment": "enroll",
    "enrolling": "enroll",
    "enrolled": "enroll",
    "enroll": "enroll",
    "enrol": "enroll",
    "schedules": "sched",
    "schedule": "sched",
    "tuition": "tuition",
    "tuitions": "tuition",
}


def _stem(word: str) -> str:
    """
    Fold one word to a coarse stem for grouping purposes only.

    Never used for retrieval or display — only to decide whether two questions
    are "the same topic" in the admin's queue.
    """
    w = word.lower()

    # Explicit table first: it exists precisely for the words where mechanical
    # suffix-stripping produces a stem that no longer matches its own root.
    if w in _STEM_OVERRIDES:
        return _STEM_OVERRIDES[w]

    w = _TAGALOG_PREFIX_HYPHEN.sub("", w, count=1) or w
    w = w.replace("-", "")
    if w in _STEM_OVERRIDES:
        return _STEM_OVERRIDES[w]

    stripped = _TAGALOG_PREFIX_BARE.sub("", w, count=1)
    if stripped and stripped in _STEM_OVERRIDES:
        return _STEM_OVERRIDES[stripped]

    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= _MIN_STEM:
            return w[: -len(suf)]
    return w



def normalize_question(question: str) -> str:
    """
    Reduce a question to its meaning-bearing word stems, sorted.

    Sorting makes word order irrelevant, which is what allows "requirements for
    enrollment" and "enrollment requirements" to group together. Returns "" when
    nothing meaningful survives (e.g. "hello po"), and callers skip those.
    """
    words = [w.lower() for w in _WORD.findall(question or "")]
    keep = []
    for w in words:
        if w in _STOPWORDS:
            continue
        stem = _stem(w)
        # Re-check after stemming: "mag" alone, or a stem that turned into a
        # stopword, carries no topic information.
        if len(stem) < 3 or stem in _STOPWORDS:
            continue
        keep.append(stem)
    if not keep:
        return ""
    # De-duplicate while keeping it deterministic.
    return " ".join(sorted(set(keep)))



def _display_topic(question: str) -> str:
    """A human-readable label: the student's own words, trimmed."""
    q = " ".join((question or "").split())
    return q[:160]


# ============================================================
# STORAGE
# ============================================================

def _empty_store() -> dict:
    return {"gaps": {}, "updated_at": ""}


# Storage is delegated to `rag.store` so the same file can become a shared
# DynamoDB item (STORE_BACKEND=dynamodb) without touching anything below.
# For gaps the multi-instance failure is quieter than for conflicts but just as
# damaging: each container would count only the questions it happened to serve,
# so "asked 14 times" reads as "asked 5 times" and the topic drops below the
# admin's attention threshold. A frequency ranking that under-reports is worse
# than no ranking, because it looks authoritative.
_store_obj = None
_store_obj_path = None
_lock_holder_lock = threading.Lock()


def _store():
    global _store_obj, _store_obj_path
    path = os.getenv("CONTENT_GAPS_FILE", GAPS_FILE)
    with _lock_holder_lock:
        if _store_obj is None or _store_obj_path != path:
            _store_obj = JsonBlobStore("gaps", path, _empty_store)
            _store_obj_path = path
        return _store_obj


def _load() -> dict:
    data = _store().load()
    # The old reader required a "gaps" key before trusting the file; keep that
    # guard so a hand-edited or foreign document degrades to empty rather than
    # raising KeyError deep inside the admin list.
    return data if "gaps" in data else _empty_store()


def _save(store: dict) -> None:
    _store().save(store)



def _prune(gaps: dict) -> dict:
    """Keep the file bounded, dropping the least-asked resolved rows first."""
    if len(gaps) <= MAX_GAPS:
        return gaps
    ordered = sorted(
        gaps.items(),
        key=lambda kv: (
            kv[1].get("status") == "resolved",
            -int(kv[1].get("count", 0)),
            kv[1].get("last_asked", ""),
        ),
    )
    return dict(ordered[:MAX_GAPS])


# ============================================================
# PUBLIC API
# ============================================================

def record_gap(
    question: str,
    *,
    answer: str = "",
    asked_by: str = "",
    conv_id: str = "",
) -> Optional[dict]:
    """
    Log one unanswered question. Returns the updated group, or None if the
    question carried no meaningful content.

    Never raises: this runs inside the chat streaming path, and a logging
    failure must not cost the student their answer.
    """
    key = normalize_question(question)
    if not key:
        return None

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with _lock:
            store = _load()
            gaps = store.setdefault("gaps", {})
            entry = gaps.get(key)

            if entry is None:
                entry = {
                    "key": key,
                    "topic": _display_topic(question),
                    "count": 0,
                    "examples": [],
                    "first_asked": now,
                    "last_asked": now,
                    "status": "open",          # open | resolved
                    "resolved_at": "",
                    "resolved_by": "",
                    "note": "",
                }

            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last_asked"] = now

            # A previously-fixed topic being asked again means the fix did not
            # land — reopen it rather than silently incrementing a resolved row.
            if entry.get("status") == "resolved":
                entry["status"] = "open"
                entry["note"] = (
                    (entry.get("note", "") + " ")
                    + "[Asked again after being marked resolved.]"
                ).strip()

            example = {
                "question": _display_topic(question),
                "answer": (answer or "")[:300],
                "asked_by": asked_by,
                "conv_id": conv_id,
                "at": now,
            }
            examples = entry.get("examples", [])
            if not any(e.get("question") == example["question"] for e in examples):
                examples.insert(0, example)
            entry["examples"] = examples[:MAX_EXAMPLES_PER_GAP]

            gaps[key] = entry
            store["gaps"] = _prune(gaps)
            _save(store)
            return entry
    except Exception as e:
        print(f"  [gaps] could not record gap (non-fatal): {e}")
        return None


def list_gaps(status: Optional[str] = None) -> List[dict]:
    """
    All gap groups, most-asked first — the admin's priority order.
    `status` may be "open" or "resolved".
    """
    store = _load()
    items = list(store.get("gaps", {}).values())
    if status:
        items = [g for g in items if g.get("status") == status]
    items.sort(
        key=lambda g: (-int(g.get("count", 0)), g.get("last_asked", "")),
        reverse=False,
    )
    return items


def gap_stats() -> dict:
    """Headline numbers for the dashboard."""
    items = list(_load().get("gaps", {}).values())
    open_items = [g for g in items if g.get("status") != "resolved"]
    return {
        "total_topics": len(items),
        "open_topics": len(open_items),
        "total_questions": sum(int(g.get("count", 0)) for g in items),
        "open_questions": sum(int(g.get("count", 0)) for g in open_items),
    }


def set_gap_status(key: str, status: str, *, resolved_by: str = "", note: str = "") -> Optional[dict]:
    """Mark a topic resolved (document uploaded) or reopen it."""
    if status not in ("open", "resolved"):
        return None
    with _lock:
        store = _load()
        entry = store.get("gaps", {}).get(key)
        if not entry:
            return None
        entry["status"] = status
        if status == "resolved":
            entry["resolved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            entry["resolved_by"] = resolved_by
        else:
            entry["resolved_at"] = ""
            entry["resolved_by"] = ""
        if note:
            entry["note"] = note.strip()[:500]
        store["gaps"][key] = entry
        _save(store)
        return entry


def delete_gap(key: str) -> bool:
    """Remove a topic entirely (e.g. spam or a test question)."""
    with _lock:
        store = _load()
        if key in store.get("gaps", {}):
            del store["gaps"][key]
            _save(store)
            return True
    return False
