"""Select and bound optional query rewrites without changing retrieval scoring."""

from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional

from rag.spelling import has_misspelling, has_unresolved_spelling_hint
from rag.unit_identity import request_text


_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "am", "do",
    "does", "did", "what", "which", "who", "whom", "whose", "when", "where",
    "why", "how", "and", "or", "but", "if", "then", "than", "of", "in", "on",
    "at", "to", "for", "with", "by", "from", "about", "as", "that", "this",
    "these", "those", "there", "here", "it", "its", "can", "could", "should",
    "would", "will", "shall", "may", "might", "must", "have", "has", "had",
    "you", "your", "me", "my", "i", "we", "our", "us", "they", "them", "their",
    "please", "tell", "give", "show", "list", "all", "any", "some", "also",
}
THIN_QUERY_TERMS = 3
_FOLLOWUP_MARKERS = (
    "what about", "how about", "and for", "what if", "same for", "the same",
    "that one", "those ones", "it instead", "instead of that",
)
_AFFIRMATION_STARTERS = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright", "fine",
    "no", "nope", "nah", "got", "great",
})
_ANAPHORA = (
    "it", "its", "this", "that", "these", "those", "they", "them", "their",
    "he", "she", "him", "her", "his", "hers", "one", "ones", "there",
)


def _content_terms(query: str) -> List[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-']+", (query or "").lower())
    return [word for word in words if word not in _STOPWORDS and len(word) > 2]


def _has_anaphora(query: str) -> bool:
    words = re.findall(r"[a-z']+", (query or "").lower())
    return any(word in _ANAPHORA for word in words)


def overview_topic(question: str) -> str:
    """Recognize short standalone topics, not scoped questions or references."""
    from rag.unit_identity import requested_unit
    from rag.source_policy import institution_history_queries

    if institution_history_queries(question):
        return ""
    text = request_text(question or "")
    text = re.sub(r"^(?:tell me about|information (?:on|about)|info (?:on|about)|about)\s+", "", text, flags=re.I)
    if not re.fullmatch(r"[A-Za-z][A-Za-z'\u2019 -]*", text):
        return ""
    words = re.findall(r"[a-z]+", text.lower())
    if not 1 <= len(words) <= 3 or _has_anaphora(text):
        return ""
    excluded = (_STOPWORDS | _AFFIRMATION_STARTERS | {
        "hello", "hi", "thanks", "help", "explain", "find", "describe", "compare", "define",
        "current", "latest", "today", "now", "total", "count", "number", "location", "locations", "hours",
        "requirement", "requirements", "criteria", "eligibility", "grade", "grades", "gwa", "gpa", "schedule", "schedules",
        "assignment", "assignments", "balance", "records", "record", "profile",
        "admission", "admissions", "enrollment", "enrolment", "scholarship", "scholarships", "tuition", "fees",
        "honor", "honors", "honour", "honours", "program", "programs", "course", "courses", "degree", "degrees",
        "bachelor", "master", "doctor", "associate",
        "departments", "colleges", "dean", "deans", "principal", "principals", "president", "officials",
    })
    if any(word in excluded for word in words):
        return ""
    unit = requested_unit(text)
    if unit and unit.protected:
        return ""
    return text.strip()


def should_optimize(question: str, history: Optional[List[Dict[str, str]]]) -> bool:
    """Match the reference gate: misspellings, thin questions, and follow-ups."""
    query = (question or "").strip()
    if not query:
        return False
    query = request_text(query)
    terms = _content_terms(query)
    # Local correction rejects ambiguous consonant substitutions. A strong
    # hint can still justify interpretation without automatically rewriting it.
    if has_misspelling(query) or has_unresolved_spelling_hint(query):
        return True
    if not history:
        return len(terms) < THIN_QUERY_TERMS
    lowered = query.lower()
    first_word = re.split(r"[\s,!.?]+", lowered)[0] if lowered else ""
    if first_word in _AFFIRMATION_STARTERS:
        return True
    if any(marker in lowered for marker in _FOLLOWUP_MARKERS):
        return True
    if _has_anaphora(query):
        return True
    return len(terms) < THIN_QUERY_TERMS


# Bound the optional wait, not the lifetime of a running provider call.
OPTIMIZER_BUDGET_SECONDS = float(os.getenv("SC_OPTIMIZER_BUDGET_SECONDS", "2.5"))


class Deadline:
    """
    A shrinking time budget.

    The natural question remains searchable if the rewrite misses this budget.
    Running provider calls keep their pool slot until completion; this bounds
    the wait in retrieval rather than pretending cancellation stops that work.
    """

    def __init__(self, seconds: float = OPTIMIZER_BUDGET_SECONDS,
                 clock=time.monotonic):
        # A monotonic clock, not wall time: an NTP correction mid-request must
        # not make a deadline expire (or never expire).
        self._clock = clock
        self._deadline = clock() + max(0.0, seconds)

    def remaining(self) -> float:
        """Seconds left, never negative (so it is safe to pass to `wait()`)."""
        return max(0.0, self._deadline - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0


def pick_search_query(literal: str, rewrite: Optional[str]) -> str:
    """
    Which string retrieval should treat as its primary query.

    The rewrite wins when it exists and says something — it is usually richer in
    the exact tokens BM25 wants. Otherwise the student's own words are used,
    which is also what happens when the rewrite arrived too late or the optimizer
    was skipped entirely.

    A rewrite that is empty, whitespace, or identical to the literal question is
    treated as absent: the optimizer sometimes echoes the input back, and letting
    that through would add a duplicate probe for no recall.
    """
    lit = (literal or "").strip()
    rw = (rewrite or "").strip()

    if not rw or rw.lower() == lit.lower():
        return lit
    return rw


class Timings:
    """
    Where the wall-clock went, for one request.

    This exists because "the assistant feels slow" is not actionable and because
    a speed change that is not measured is a guess. One line in the log per
    answer makes the next person's version of this task start from numbers:

        [timing] optimizer=skipped retrieval=0.63s rerank=3.11s total=3.79s

    Failure here must never cost an answer, so `stop()` on an unknown phase is
    silently ignored rather than raising — an instrumentation bug that broke a
    student's question would be a worse bug than the slowness it was added to
    investigate.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._start = clock()
        self._open: Dict[str, float] = {}
        self.marks: Dict[str, float] = {}
        self.notes: Dict[str, str] = {}

    def start(self, phase: str) -> None:
        self._open[phase] = self._clock()

    def stop(self, phase: str) -> None:
        began = self._open.pop(phase, None)
        if began is None:
            return
        # Accumulate rather than overwrite: a phase entered twice (a retry) has
        # cost the student both attempts, and reporting only the second would
        # hide exactly the case worth seeing.
        self.marks[phase] = self.marks.get(phase, 0.0) + (self._clock() - began)

    def note(self, phase: str, text: str) -> None:
        """Record a phase that had no duration, e.g. 'optimizer=skipped'."""
        self.notes[phase] = text

    def total(self) -> float:
        return self._clock() - self._start

    def render(self) -> str:
        parts = [f"{k}={v}" for k, v in self.notes.items()]
        parts += [f"{k}={v:.2f}s" for k, v in self.marks.items()]
        parts.append(f"total={self.total():.2f}s")
        return "[timing] " + " ".join(parts)
