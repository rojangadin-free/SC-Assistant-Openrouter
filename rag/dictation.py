"""
rag/dictation.py — repairing what a speech recogniser hands you.

Why this is a retrieval problem, not a UI problem
-------------------------------------------------
A microphone button is five lines of JavaScript. The reason this module exists is
what arrives *after* the student stops talking.

`SpeechRecognition` (and every phone keyboard's dictation, which is how most
students already "type") returns unpunctuated, lowercase text, and it spells out
acronyms it does not know as separate letters:

    spoken:      "what is b s i t"
    typed:       "what is BSIT"

Those two are not slightly different, they are catastrophically different. Run
them through the pipeline in `rag/chain.py`:

    _content_terms("what is b s i t")  ->  []        # every token < 3 chars
    _content_terms("what is BSIT")     ->  ["bsit"]

BM25 scores a query of four single characters at essentially zero — those tokens
appear in no document — and the dense encoder embeds "b s i t" nowhere near
"Bachelor of Science in Information Technology". The question retrieves nothing
useful, the assistant admits ignorance, and `record_gap()` files it as a missing
document. The corpus was fine. The microphone was the bug, and the content-gap
report would have sent an admin looking for a page that already exists.

So voice input without this module does not merely answer worse; it actively
pollutes the admin's "documents to add" list.

Deliberately not gated on a flag
--------------------------------
The obvious design is for the mic button to set `is_voice=True` and for the
server to repair only those messages. That was rejected: the most common way a
Samar College student dictates is the *Android keyboard's* mic, which produces an
ordinary typed message as far as the browser is concerned. A flag would repair
our own button and miss the majority of real dictation.

Instead the repair is inferred from the text and is **additive**: a variant is
only produced when it actually differs from what the student sent, and the
original wording is always kept and ranked first (same contract as
`rag/language.py`). Typed input therefore costs nothing and cannot be corrupted —
the worst case for a false positive is one extra retrieval probe.

Nothing here rewrites the student's message. The transcript they see is the
transcript they spoke; only the *search* gets the repaired copy.
"""

from __future__ import annotations

import re
from typing import List

# --------------------------------------------------------------------------- #
# 1. Spelled-out letter runs
# --------------------------------------------------------------------------- #
#
# The single highest-value repair, and the one with a measurable before/after.
# Recognisers emit unknown acronyms letter by letter: "b s i t", "c i t a s",
# "s c t i", "m a e d". Three or more isolated single letters in a row is not
# something anyone types, so collapsing them is safe.
#
# Two letters is NOT enough to act on: "is a b" and Waray/Tagalog particles would
# start colliding, and a two-letter acronym is rare enough not to be worth that.
_LETTER_RUN = re.compile(r"\b(?:[a-z]\s+){2,}[a-z]\b", re.I)

# "b.s.i.t." and "b-s-i-t" — the same failure with punctuation the recogniser
# guessed at. Handled separately because the separator is not whitespace.
_PUNCTUATED_ACRONYM = re.compile(r"\b(?:[a-z][.\-]){2,}[a-z]\.?\b", re.I)


def _collapse_letter_runs(text: str) -> str:
    text = _PUNCTUATED_ACRONYM.sub(
        lambda m: re.sub(r"[.\-]", "", m.group(0)), text
    )
    return _LETTER_RUN.sub(lambda m: re.sub(r"\s+", "", m.group(0)), text)


# --------------------------------------------------------------------------- #
# 2. Fillers
# --------------------------------------------------------------------------- #
#
# Speech contains hesitation that typing does not. These add tokens to the BM25
# query that match nothing, which dilutes the terms that do matter.
#
# Kept short on purpose: every entry is a word that carries no meaning ANYWHERE
# in a question. "like" and "so" are excluded — "so" appears in program names and
# "like" is load-bearing in "programs like BSIT".
_FILLERS = {
    "uh", "uhh", "um", "umm", "erm", "ah", "ahh", "eh", "hmm", "mmm",
}

# Recognisers sometimes transcribe the *name* of a punctuation mark instead of
# inserting it, especially when the student says it out loud.
_SPOKEN_PUNCTUATION = {
    "question mark": "?",
    "full stop": ".",
    "period": ".",
    "comma": ",",
}


# --------------------------------------------------------------------------- #
# 3. Institution-specific mishearings
# --------------------------------------------------------------------------- #
#
# A general-purpose recogniser has never heard of Samar College, so its language
# model replaces local proper nouns with whatever English words are acoustically
# closest. Every entry below is a substitution the model makes *because* the real
# term is absent from its vocabulary — which is exactly the set of terms this
# corpus is about.
#
# This table is the one part of this module that is institution-specific, and it
# is data rather than logic so it can grow without touching anything else.
_MISHEARD = {
    # Samar
    "summer college": "samar college",
    "sumer college": "samar college",
    "samar colleges": "samar college",
    "somer college": "samar college",
    # CITAS — College of Information Technology and Allied Sciences
    "sitas": "citas",
    "see tas": "citas",
    "seatass": "citas",
    "cetas": "citas",
    "kitas": "citas",
    # SCTI — Samar College Technological Institute
    "skity": "scti",
    "sky tee": "scti",
    "escti": "scti",
    # Programs
    "b sit": "bsit",
    "bee sit": "bsit",
    "be sit": "bsit",
    "bs it": "bsit",
    "bs ed": "bsed",
    "be ed": "beed",
    "mad": "maed",
    "may ed": "maed",
    # Money / process words that a Waray accent commonly shifts
    "tuition pee": "tuition fee",
    "enrolment": "enrollment",
    "enrolement": "enrollment",
}

# Longest first, so "see tas" is not half-matched by a shorter key.
_MISHEARD_KEYS = sorted(_MISHEARD, key=len, reverse=True)


def _fix_misheard(text: str) -> str:
    for wrong in _MISHEARD_KEYS:
        text = re.sub(rf"\b{re.escape(wrong)}\b", _MISHEARD[wrong], text)
    return text


# --------------------------------------------------------------------------- #
# Diagnosis (exported for the UI and for tests)
# --------------------------------------------------------------------------- #

# A dictated sentence is long enough to have earned punctuation and has none.
_MIN_DICTATED_WORDS = 4


def looks_dictated(text: str) -> bool:
    """
    A cheap, honest guess at whether this text came out of a microphone.

    NOT used to gate the repair — `dictation_variants()` is self-limiting, and a
    gate here would mean a wrong guess silently disables the fix. It exists so
    the UI can say "we cleaned that up" and so the behaviour is testable in
    isolation.

    Signals, in order of reliability:
      1. A spelled-out letter run. Nobody types "b s i t"; this alone is decisive.
      2. Four or more words with no sentence punctuation at all AND no capital
         letter — the signature of a recogniser that punctuates nothing.
    """
    t = (text or "").strip()
    if not t:
        return False

    if _LETTER_RUN.search(t):
        return True

    words = t.split()
    if len(words) < _MIN_DICTATED_WORDS:
        return False

    has_punctuation = bool(re.search(r"[.?!,;:]", t))
    has_capital = any(c.isupper() for c in t)
    return not has_punctuation and not has_capital


# --------------------------------------------------------------------------- #
# The repair
# --------------------------------------------------------------------------- #

def repair_transcript(text: str) -> str:
    """
    Return `text` with dictation artefacts removed. Idempotent, and a no-op on
    text that has none — so calling it on typed input is harmless.

    Order matters:
      1. spoken punctuation becomes punctuation, before anything counts words
      2. letter runs collapse, before mishearings are matched (so that "b s i t"
         has already become "bsit" and does not need its own table entry)
      3. mishearings, on the now-collapsed text
      4. fillers, last, because removing them earlier would let two halves of a
         letter run ("b uh s i t") join up in a way the student never said
    """
    t = (text or "").strip()
    if not t:
        return ""

    lowered = t.lower()

    for spoken, mark in _SPOKEN_PUNCTUATION.items():
        lowered = re.sub(rf"\s+{re.escape(spoken)}\b", mark, lowered)

    lowered = _collapse_letter_runs(lowered)
    lowered = _fix_misheard(lowered)

    kept = [w for w in lowered.split() if w.strip(".,?!") not in _FILLERS]
    lowered = " ".join(kept)

    return re.sub(r"\s+([.,?!])", r"\1", lowered).strip()


def dictation_variants(text: str) -> List[str]:
    """
    The retrieval probes to ADD for a possibly-dictated question.

    Returns `[]` when the repair changed nothing, which is the common case and
    the reason this is safe to run on every question. Compared
    case-insensitively so that merely lowercasing a typed sentence — which
    changes the string but not a single retrieval token — does not buy a
    redundant round-trip to Pinecone.
    """
    t = (text or "").strip()
    if not t:
        return []

    repaired = repair_transcript(t)
    if not repaired or repaired == t.lower():
        return []

    return [repaired]
