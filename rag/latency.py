"""Bound contextual interpretation without confusing known words with correct intent.

Every text question gets an interpretation opportunity, including real-word
typos. The optimizer overlaps other preparation, but accepted wording is
collected before retrieval. Late results never mutate an answer in progress.
"""

from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional

def should_optimize(question: str, history: Optional[List[Dict[str, str]]]) -> bool:
    """Known words can still express a typo; skip only inputs without text."""
    return bool(re.search(r"\w", question or "", re.UNICODE))


# One bounded interpretation call, never an unbounded second round trip.
OPTIMIZER_BUDGET_SECONDS = float(os.getenv("SC_OPTIMIZER_BUDGET_SECONDS", "8.0"))


class Deadline:
    """
    A shrinking time budget.

    Bounds how long preparation waits for interpretation before safe fallback.
    Running work cannot always be cancelled, so late results are ignored; the
    model client's separate timeout also bounds occupied worker slots.
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
