"""
rag/latency.py — the two decisions that make an answer start sooner.

Nothing here touches indexing, retrieval scoring or the prompt. This module owns
only *when* work is allowed to happen, and it is deliberately pure string /
arithmetic work so it can be tested with no Pinecone index, no model weights and
no network — the same reasoning as rag/dictation.py.

The problem being fixed
----------------------
`sc_assistant/chat.py` cannot yield a single SSE token until `app_graph.invoke()`
returns. Inside that call, three things happen strictly in order:

    1. the query-optimizer LLM rewrites the question   (network, blocking)
    2. Pinecone is searched                            (network)
    3. the cross-encoder reranks the candidates        (CPU)

Step 1 is a full round-trip to a *reasoning* model on a *free* tier to produce
about ten words, and steps 2-3 cannot start until it lands. The student is
looking at a blank screen for the whole of it.

Two observations make most of that cost removable:

  A. The rewrite is only worth waiting for when the question cannot be understood
     on its own. "what about for transferees?" needs the previous turn to become
     a searchable query; "who is the dean of education" does not — it already
     contains every term the index needs, and `retrieve_documents()` ranks the
     student's literal words anyway.

  B. When the rewrite *is* wanted, it does not have to be waited for *first*.
     Retrieval on the literal question can be in flight while the rewriter is
     still thinking, and the rewrite can join as one more probe if it arrives in
     time. `multi_query_retrieve()` already fans out to several phrasings, so an
     extra one costs a parallel network call, not a serial one.

`should_optimize()` decides A. `Deadline` bounds B.

What this module refuses to do
------------------------------
It does not decide which documents win. Every function here either says "skip
work that would not have changed the outcome" or "stop waiting for work that is
late". A bug in this file can make an answer slower, or make it use the literal
question instead of a rewrite — it cannot reorder a ranking, which is why none of
it needed to be validated against tools/eval_retrieval.py.
"""

from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional

# Words that carry no search signal. Same intent as `_STOPWORDS` in rag/chain.py
# and duplicated on purpose: importing chain.py constructs the Pinecone index and
# loads the embedding model at module scope, which would make a unit test of this
# file need credentials and ~400 MB of weights. The list only has to be good
# enough to count how much *content* a question carries.
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

# Below this many content words a question is too thin to search on its own, so
# the rewrite is worth waiting for.
#
# Three, not four — and the difference was found by testing, not by taste.
# `rag/chain.py` uses 4 for `expand_queries()`, but that threshold answers a
# different question: "should I bolt 'complete list of' onto this?", where a
# false positive costs one extra Pinecone probe. Here a false positive costs an
# entire LLM round-trip in front of every answer, so the bar has to be lower.
#
# What 4 actually rejected:
#
#   "who is the dean of the college of education"  -> [dean, college, education]
#   "who is the principal of the elementary dept"  -> [principal, elementary,
#                                                      department]
#
# Both are completely searchable — they name their subject and their unit — and
# both are the *most common shape of question this assistant receives*. At 4 they
# measured as thin and paid for a rewrite they had no use for, which would have
# left the headline case of this whole change still slow.
THIN_QUERY_TERMS = 3


# Phrases that only mean something relative to a previous turn. A question
# containing one of these is a follow-up no matter how many content words it has,
# because the words it carries may be about the wrong subject:
#
#   "what about for transferees?"        -> refers to the previous topic
#   "is that the same for shs?"          -> "that" is in the last answer
#
# Sending those to retrieval unrewritten searches for the wrong thing, which is a
# wrong answer, not a slow one. So this list biases toward waiting.
_FOLLOWUP_MARKERS = (
    "what about", "how about", "and for", "what if", "same for", "the same",
    "that one", "those ones", "it instead", "instead of that",
)

# Pronouns and deictics that need an antecedent. Checked as whole words so
# "their" in "their programs" counts but "that" inside "thatch" does not.
_ANAPHORA = (
    "it", "its", "this", "that", "these", "those", "they", "them", "their",
    "he", "she", "him", "her", "his", "hers", "one", "ones", "there",
)


def _content_terms(query: str) -> List[str]:
    """The words a search engine would actually match on."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-']+", (query or "").lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def _has_anaphora(query: str) -> bool:
    """True when the question points at something it does not name."""
    words = re.findall(r"[a-z']+", (query or "").lower())
    return any(w in _ANAPHORA for w in words)


def should_optimize(question: str, history: Optional[List[Dict[str, str]]]) -> bool:
    """
    Should the query-optimizer LLM be called for this question?

    Returns False only when the question is self-contained enough that a rewrite
    could not change which documents are found. That is the common case for a
    first message, and skipping it removes an entire LLM round-trip from the time
    the student spends waiting.

    The rule, in the order it is applied:

      1. No history          -> nothing to resolve a reference against, so the
                                rewrite has no information the question lacks.
                                Skip, UNLESS the question is too thin to search.
      2. Follow-up markers   -> wait. "what about for transferees" is meaningless
         or a bare pronoun      alone, and searching it unrewritten retrieves the
                                wrong subject. A wrong answer is worse than a
                                slow one, so ambiguity resolves toward waiting.
      3. Thin question       -> wait. Fewer than THIN_QUERY_TERMS content words
                                is where retrieval measurably struggles.
      4. Otherwise           -> skip. The question names its own subject and
                                carries enough terms; the literal words are
                                already retrieved and reranked.

    Note that (1) is checked before (2): a first-turn question containing "that"
    has nothing to resolve it against, so waiting for a rewrite buys nothing.
    """
    q = (question or "").strip()
    if not q:
        return False

    terms = _content_terms(q)

    # (1) First turn. There is no previous topic to inherit, so the only thing a
    # rewrite can add is bulk — and `expand_queries()` already does that for
    # thin queries without a network call.
    if not history:
        return len(terms) < THIN_QUERY_TERMS

    lowered = q.lower()

    # (2) An explicit continuation, or a reference with no antecedent in the
    # sentence itself.
    if any(marker in lowered for marker in _FOLLOWUP_MARKERS):
        return True
    if _has_anaphora(q):
        return True

    # (3) Too little to search on.
    if len(terms) < THIN_QUERY_TERMS:
        return True

    # (4) Self-contained mid-conversation question: "who is the dean of
    # education" means the same thing on turn 1 and turn 9.
    return False


# How long retrieval may wait for a late rewrite before going ahead with the
# documents it already has. Chosen to be shorter than the round-trip it is
# guarding: if the optimizer is slower than this, waiting for it costs more than
# the recall it might add. Env-overridable so a deployment on a slow link can
# raise it without a code change.
OPTIMIZER_BUDGET_SECONDS = float(os.getenv("SC_OPTIMIZER_BUDGET_SECONDS", "2.5"))


class Deadline:
    """
    A shrinking time budget.

    Used for the one thing in the answer path that is allowed to be abandoned:
    the optimizer's rewrite. Retrieval starts on the student's literal question
    immediately; when the rewrite is wanted, this bounds how long the request
    will wait for it before proceeding without it.

    Deliberately not a hard timeout on the LLM call. Cancelling the call would
    waste the tokens already generated and lose a rewrite that was 50 ms away
    from being useful. `remaining()` instead lets the *waiter* give up while the
    call finishes in the background, so a rewrite that lands late is simply
    ignored rather than cancelled.
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
