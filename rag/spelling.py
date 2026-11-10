"""Corpus-derived spelling candidates and conservative offline corrections.

Document indexing updates spell_vocab.json; new terms do not require Python
changes. Workers notice changes to that file without restarting. Seed terms
provide a small fallback when the deployment has no vocabulary yet.

Local edits are deliberately limited. Valid words, names, short words and
uncertain spellings need sentence-level interpretation, not a growing table of
typo pairs or academic phrases. rag.query_optimizer handles that on every text
question, using the original wording plus these non-authoritative candidates.
Only the working question changes; displayed input and history remain intact.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple

# Where the corpus vocabulary lives. Next to bm25_values.json, because it is the
# same kind of artefact: derived from the indexed documents, rebuilt when they
# change, and useless without them.
SPELL_VOCAB_FILE = os.getenv("SC_SPELL_VOCAB", "spell_vocab.json")

# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #

# Below this length a typo and a real word are indistinguishable: "cum" is one
# letter from "sum", "cut" and "cup", and "an"/"am"/"as" would thrash forever.
# Four is also comfortably below every campus term worth repairing
# ("fees", "unit", "form" are all already in the vocabulary).
MIN_CORRECTABLE_LEN = 4

# A word shorter than this may only be repaired by INSERTING a letter the
# student dropped — never by substituting one letter for another.
#
# Found by sweeping real questions, same as everything else in this file:
#
#   "who do i TALK to about guidance"  -> "task"   ("talk" appears in no
#                                                   document; "task" does, one
#                                                   substitution away)
#   "requirements PARA sa transferee"  -> "part"   (Waray — rag/language.py
#                                                   owns that word)
#
# At four or five letters a single substitution reaches a completely unrelated
# word, and both of those repairs actively destroy the query. A longer word has
# no such neighbours: nothing in the corpus is one edit from "requirements".
#
# Insertion stays allowed at any length because it is far more constrained —
# the typed letters all have to survive, in order:
#
#   "dens"  -> "deans"      d-e-_-n-s, one letter dropped   (kept)
#   "talk"  -> "task"       l -> s, a different word        (rejected)
#
# One exception: a vowel-for-vowel swap, see `_vowel_slip_only()`. That is what
# "honir" -> "honor" is, and it is the headline case of this whole module.
SHORT_SUBSTITUTION_LEN = 6

# Used by `_vowel_slip_only()`. A word's consonants are what identify it;
# vowels are where the slips happen.
_VOWELS = frozenset("aeiouy")





# How many single-character edits (insert / delete / substitute / transpose) a
# repair may span. One for a short word, two once a word is long enough that two
# edits still leave most of it intact ("enrolmnet" -> "enrollment").
MAX_EDITS_SHORT = 1
MAX_EDITS_LONG = 2
LONG_WORD_LEN = 7

# A correction target must appear at least this often in the corpus. OCR of a
# scanned page invents words ("thc", "arid", "rcgistrar"); requiring a few
# sightings keeps one bad page from becoming a spell-checker suggestion.
MIN_TARGET_FREQ = 3

# Frequency credited to a baked-in seed term that the corpus does not confirm.
# Above MIN_TARGET_FREQ (so seeds are usable) but below anything a real document
# term reaches (so the corpus always wins a tie).
SEED_FREQ = MIN_TARGET_FREQ

# Limit unreviewed local edits. The contextual optimizer can repair more words
# together; it is not subject to this conservative fallback's limit.
MAX_CORRECTIONS = 3


# --------------------------------------------------------------------------- #
# The seed vocabulary
# --------------------------------------------------------------------------- #
#
# Campus and academic terms, i.e. the words a registrar document is made of.
# This is a floor, not the whole vocabulary: `spell_vocab.json` adds everything
# the real documents contain. Entries here are the terms whose misspelling is
# most expensive — the ones that decide WHICH page is the right page.
_SEED_TERMS: Tuple[str, ...] = (
    # ---- Latin honors: the case that started this module -------------------
    "latin", "honor", "honors", "honour", "honours", "laude", "cum",
    "magna", "summa", "distinction", "awards", "awardee", "valedictorian",
    "salutatorian", "dean's", "deans", "medal", "recognition", "citation",
    "graduate", "graduates", "graduating", "graduation", "commencement",
    "candidate", "candidates",

    # ---- Enrollment / registrar -------------------------------------------
    "enrollment", "enrolment", "enroll", "enrolled", "enrolling", "registration",
    "register", "registrar", "admission", "admissions", "requirements",
    "requirement", "documents", "credentials", "transcript", "records",
    "diploma", "certificate", "clearance", "application", "applicant",
    "transferee", "transferees", "transfer", "shifting", "shiftee", "returning",
    "freshman", "freshmen", "sophomore", "junior", "senior", "undergraduate",
    "student", "students", "enrollee", "evaluation", "assessment", "advising",

    # ---- Money -------------------------------------------------------------
    "tuition", "fees", "fee", "payment", "payments", "balance", "installment",
    "downpayment", "discount", "scholarship", "scholarships", "scholar",
    "grant", "subsidy", "penalty", "surcharge", "refund", "cashier",
    "assessment", "miscellaneous", "laboratory", "matriculation",

    # ---- Academics ---------------------------------------------------------
    "course", "courses", "program", "programs", "curriculum", "subject",
    "subjects", "units", "semester", "semestral", "trimester", "midterm",
    "finals", "prelim", "prelims", "grade", "grades", "grading", "average",
    "computation", "passing", "failing", "incomplete", "removal", "retention",
    "probation", "dismissal", "dropping", "overload", "residency", "prospectus",
    "schedule", "syllabus", "attendance", "examination", "examinations",
    "entrance", "qualifying", "comprehensive", "thesis", "capstone",
    "internship", "practicum", "immersion", "deployment", "seminar",

    # ---- People and places -------------------------------------------------
    "dean", "deans", "principal", "president", "chairperson", "coordinator",
    "faculty", "instructor", "professor", "teacher", "adviser", "counselor",
    "guidance", "library", "clinic", "canteen", "campus", "department",
    "college", "colleges", "school", "institute", "office", "administration",

    # ---- Institution-specific ---------------------------------------------
    "samar", "catbalogan", "waray", "scti", "citas", "bsit", "bsed", "beed",
    "bsba", "bscs", "bsais", "maed", "shs", "jhs", "senior", "highschool",
    "elementary", "preschool", "kindergarten",

    # ---- Policy / conduct --------------------------------------------------
    "handbook", "policy", "policies", "regulations", "guidelines", "discipline",
    "disciplinary", "offense", "offenses", "sanction", "suspension", "uniform",
    "identification", "violation", "misconduct", "grievance", "appeal",
    "organization", "activities", "calendar", "holiday", "suspended",
)

# Words that are unremarkable English and must never be "corrected" — but are
# NOT offered as corrections either, because a document that never uses them has
# no business pulling a student's typo toward them.
#
# Kept short and generic on purpose: this is the glue of a question, not its
# subject. Anything with retrieval value belongs in the corpus vocabulary.
_COMMON_ENGLISH = frozenset("""
about above after again against all also always am an and another any anyone
anything are aren't around as ask asked asking at back be because been before
being below best better between both but by came can cannot can't come could
couldn't did didn't do does doesn't doing done don't down during each either
else enough even ever every everyone everything few find first for from get
gets getting give given go goes going good got great had happen has have having
he hello help her here hers herself hi him himself his how however if in inside
into is isn't it its itself just keep kind know known last later least less let
like list long lose loses losing lost lot lots look looking made make makes
making many may maybe me mean means might

mine more most much must my myself near need needed needs never new next no none
nor not nothing now of off often on once one only or other others our ours out
outside over own part people per please possible put question questions quite
rather really right said same say says see seen send sent set she should
shouldn't since so some someone something soon still such sure take taken tell
than thank thanks that the their theirs them themselves then there these they
thing things think this those though through thus time to today together too
took total under until up upon us use used using usually very want wants was
wasn't way we well went were what when where whether which while who whom whose
why will with within without won't work would wouldn't yes yet you your yours
yourself
""".split())


# --------------------------------------------------------------------------- #
# Vocabulary loading
# --------------------------------------------------------------------------- #

# Keep Unicode words/codes whole, but do not consume closing quotes or dashes.
_WORD_RE = re.compile(r"\b[A-Za-z]\w*(?:['\-\u2010-\u2015\u2212]\w+)*(?!\w)")


def _read_vocab_file(path: str) -> Dict[str, int]:
    """
    The corpus vocabulary, or {} when it has not been built.

    Never raises. A missing or malformed file must degrade to "seed terms only"
    — the same behaviour as a fresh checkout — and never to a failed answer.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}

    terms = data.get("terms") if isinstance(data, dict) else None
    if not isinstance(terms, dict):
        return {}

    out: Dict[str, int] = {}
    for word, count in terms.items():
        if not isinstance(word, str):
            continue
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        w = word.strip().lower()
        if w:
            out[w] = n
    return out


class _Vocabulary:
    """
    The words a correction is allowed to produce, indexed for fast lookup.

    A lookup snapshot, replaced when indexing publishes a changed vocabulary.
    """

    def __init__(self, terms: Dict[str, int]):
        self.freq: Dict[str, int] = dict(terms)

        # Local edits retain the initial letter; interpreter hints can also
        # consider first-letter errors. Both searches stay length-bounded.
        self.buckets: Dict[Tuple[str, int], List[str]] = {}
        self.by_length: Dict[int, List[str]] = {}
        for word in self.freq:
            self.buckets.setdefault((word[0], len(word)), []).append(word)
            self.by_length.setdefault(len(word), []).append(word)

    def candidates(self, word: str, max_edits: int, same_initial=True) -> Iterable[str]:
        head = word[0]
        for length in range(len(word) - max_edits, len(word) + max_edits + 1):
            yield from (self.buckets.get((head, length), ()) if same_initial
                        else self.by_length.get(length, ()))


_vocab: Optional[_Vocabulary] = None
_vocab_stamp = None


def _vocabulary() -> _Vocabulary:
    global _vocab, _vocab_stamp
    try:
        stat = os.stat(SPELL_VOCAB_FILE)
        stamp = (SPELL_VOCAB_FILE, stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except OSError:
        stamp = (SPELL_VOCAB_FILE, None, None, None)
    if _vocab is None or stamp != _vocab_stamp:
        terms = {w: SEED_FREQ for w in _SEED_TERMS}
        for word, count in _read_vocab_file(SPELL_VOCAB_FILE).items():
            # The corpus wins when it has actually seen the word more often than
            # the seed floor; a seed term absent from the documents keeps its
            # floor so it stays usable.
            terms[word] = max(count, terms.get(word, 0))
        _vocab = _Vocabulary(terms)
        _vocab_stamp = stamp
        _suggest.cache_clear()
        _candidate_suggestions.cache_clear()
    return _vocab


def reload_vocabulary() -> int:
    """
    Re-read `spell_vocab.json` and return how many terms are now known.

    Called after indexing a document, and by the tests, so a vocabulary written
    by a running process takes effect without a restart.
    """
    global _vocab
    _vocab = None
    _suggest.cache_clear()
    return len(_vocabulary().freq)


def vocabulary_size() -> int:
    """How many words a correction may be drawn from. Useful in logs."""
    return len(_vocabulary().freq)


# --------------------------------------------------------------------------- #
# Edit distance
# --------------------------------------------------------------------------- #

def _edit_distance(a: str, b: str, cutoff: int) -> int:
    """
    Damerau-Levenshtein distance between two short words, abandoned early.

    Transpositions are counted as ONE edit rather than two, which matters
    because they are the single most common typing error: "hnoir", "requirments"
    and "enrolmnet" are all one slip of two fingers, and a plain Levenshtein
    would score them as two and reject the repair.

    Returns `cutoff + 1` as soon as the distance cannot come in at or under
    `cutoff` — the caller only ever asks "is this close enough", so computing
    the true distance of a hopeless pair is wasted work.
    """
    la, lb = len(a), len(b)
    if abs(la - lb) > cutoff:
        return cutoff + 1
    if a == b:
        return 0

    prev_prev: List[int] = []
    prev = list(range(lb + 1))

    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            val = min(
                prev[j] + 1,        # deletion
                cur[j - 1] + 1,     # insertion
                prev[j - 1] + cost  # substitution
            )
            if (
                i > 1 and j > 1
                and a[i - 1] == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                val = min(val, prev_prev[j - 2] + 1)  # transposition
            cur[j] = val

        if min(cur) > cutoff:
            return cutoff + 1

        prev_prev, prev = prev, cur

    return prev[lb]


def _max_edits(word: str) -> int:
    return MAX_EDITS_LONG if len(word) >= LONG_WORD_LEN else MAX_EDITS_SHORT


# Endings that turn one form of a word into another form of the SAME word.
# A correction across one of these is not a spelling repair, it is a grammar
# edit, and it buys no recall: BM25 already matches "item" against the "items"
# in a document, and the embedding does not care at all.
#
# Found by testing. "the lost item procedure" was being rewritten to "the lost
# itemS procedure" — a pointless probe that also told rag/latency.py the
# question was unsearchable and bought an LLM round-trip for a question that had
# nothing wrong with it.
_INFLECTIONS = ("s", "es", "ed", "d", "ing", "'s")


def _same_word(a: str, b: str) -> bool:
    """
    True when two spellings are inflections of each other ("item"/"items",
    "enroll"/"enrolled", "pay"/"paying").

    Crude on purpose — this is not a stemmer, it only has to recognise the
    handful of endings a student's singular/plural slip actually produces.
    """
    short, long = sorted((a, b), key=len)
    if short == long:
        return True
    if not long.startswith(short):
        return False
    return long[len(short):] in _INFLECTIONS


def _vowel_slip_only(typed: str, candidate: str) -> bool:
    """
    True when the two words are the same length and differ ONLY in vowels.

    This is the one substitution a short word is allowed, and it exists for the
    question this module was written for:

        "honir" vs "honor"   h-n-r intact, i/o differ   -> True   (repaired)
        "talk"  vs "task"    l vs s, both consonants    -> False  (left alone)
        "para"  vs "part"    a vs t                     -> False  (left alone)

    A word's consonant skeleton is what identifies it. Vowels are where the
    slips happen (adjacent keys, unstressed syllables, Waray/Tagalog spelling
    habits), and changing one almost never lands on a different real word.
    """
    if len(typed) != len(candidate):
        return False

    differences = 0
    for x, y in zip(typed, candidate):
        if x == y:
            continue
        if x not in _VOWELS or y not in _VOWELS:
            return False          # a consonant changed -> a different word
        differences += 1

    return differences > 0




# --------------------------------------------------------------------------- #
# Correction
# --------------------------------------------------------------------------- #

def is_known(word: str) -> bool:
    """Vocabulary membership, not a verdict that the word fits this sentence."""
    w = (word or "").strip().lower()
    if not w:
        return True
    return w in _COMMON_ENGLISH or w in _vocabulary().freq


# Snapshot identity prevents an in-flight old lookup from caching a result for
# a vocabulary published after it started.
@lru_cache(maxsize=4096)
def _suggest(word: str, vocab: _Vocabulary) -> Optional[str]:
    """
    The corpus word `word` was most likely meant to be, or None.

    None is returned far more often than a suggestion, and deliberately:

      * the word is already known, or too short, or not purely alphabetic;
      * nothing in the corpus is close enough;
      * TWO corpus words are equally close and equally common, so any choice
        would be a coin flip. A wrong repair is a wrong search, and the
        student's own wording is already being searched — declining costs
        nothing, guessing can cost the answer.
    """
    w = word.lower()

    if len(w) < MIN_CORRECTABLE_LEN or not w.isascii() or not w.isalpha():
        return None
    if w in _COMMON_ENGLISH or w in vocab.freq:
        return None

    cutoff = _max_edits(w)

    best: Optional[str] = None
    best_distance = cutoff + 1
    best_freq = 0
    tied = False

    # A short word may only GAIN letters. See SHORT_SUBSTITUTION_LEN: at this
    # length a one-letter swap lands on an unrelated word ("talk" -> "task")
    # about as often as it lands on the right one.
    insertions_only = len(w) < SHORT_SUBSTITUTION_LEN

    for candidate in vocab.candidates(w, cutoff):
        freq = vocab.freq.get(candidate, 0)
        if freq < MIN_TARGET_FREQ:
            continue

        # A short word may only gain letters, or swap a vowel for a vowel.
        if (
            insertions_only
            and len(candidate) <= len(w)
            and not _vowel_slip_only(w, candidate)
        ):
            continue


        distance = _edit_distance(w, candidate, cutoff)
        if distance > cutoff:
            continue


        if distance < best_distance or (distance == best_distance and freq > best_freq):
            best, best_distance, best_freq = candidate, distance, freq
            tied = False
        elif distance == best_distance and freq == best_freq and candidate != best:
            # Two corpus words, equally close and equally common.
            tied = True

    if best is None or tied:
        return None

    # "item" -> "items" is a grammar edit, not a spelling repair. BM25 already
    # matches one against the other and the probe would be a duplicate, so it
    # is dropped here rather than shipped as a correction.
    if _same_word(w, best):
        return None

    return best



def suggest(word: str) -> Optional[str]:
    """Public wrapper around the cached lookup — `str` in, `str | None` out."""
    return _suggest((word or "").strip(), _vocabulary())


def _typing_distance(word: str, candidate: str) -> float:
    """Rank hints by likely keyboard slips/omitted vowels, never apply an edit."""
    previous = [0.0]
    for char in candidate:
        previous.append(previous[-1] + (0.35 if char in _VOWELS else 1.0))
    before_previous = None
    for i, typed in enumerate(word, 1):
        deletion = 0.35 if typed in _VOWELS else 1.0
        current = [previous[0] + deletion]
        for j, meant in enumerate(candidate, 1):
            if typed == meant:
                substitution = 0.0
            elif typed in _VOWELS and meant in _VOWELS:
                substitution = 0.25
            elif any(typed + meant in row or meant + typed in row
                     for row in ("qwertyuiop", "asdfghjkl", "zxcvbnm")):
                substitution = 0.5
            else:
                substitution = 1.0
            cost = min(previous[j] + deletion,
                       current[j - 1] + (0.35 if meant in _VOWELS else 1.0),
                       previous[j - 1] + substitution)
            if (before_previous is not None and j > 1
                    and typed == candidate[j - 2] and word[i - 2] == meant):
                cost = min(cost, before_previous[j - 2] + 0.5)
            current.append(cost)
        before_previous, previous = previous, current
    return previous[-1] + (0.25 if word[0] != candidate[0] else 0.0)


@lru_cache(maxsize=2048)
def _candidate_suggestions(word: str, vocab: _Vocabulary) -> Tuple[str, ...]:
    """Wider, non-authoritative suggestions; only the interpreter may apply them."""
    cutoff = 3 if len(word) < 10 else 4
    ranked = []
    for candidate in vocab.candidates(word, cutoff, same_initial=False):
        frequency = vocab.freq.get(candidate, 0)
        if frequency < MIN_TARGET_FREQ or candidate == word or candidate in _COMMON_ENGLISH:
            continue
        distance = _edit_distance(word, candidate, cutoff)
        if distance <= cutoff:
            ranked.append((_typing_distance(word, candidate), distance, -frequency, candidate))
    return tuple(item[3] for item in sorted(ranked)[:8])


def _word_matches(text: str) -> Iterable[re.Match]:
    for match in _WORD_RE.finditer(text):
        # Python's \w excludes combining marks; never suggest a partial word.
        edges = text[max(0, match.start() - 1):match.start()] + text[match.end():match.end() + 1]
        if not any(unicodedata.category(char).startswith("M") for char in edges):
            yield match


def spelling_candidates(text: str) -> Dict[str, List[str]]:
    """Bounded corpus hints, including first-letter mistakes and rare OCR words."""
    vocab = _vocabulary()
    hints = {}
    considered = set()
    for match in _word_matches(text or ""):
        token = match.group(0)
        word = token.lower()
        if (not word.isascii() or not word.isalpha() or not 3 <= len(word) <= 24
                or token.isupper() or word in considered or word in _COMMON_ENGLISH
                or vocab.freq.get(word, 0) >= MIN_TARGET_FREQ):
            continue
        considered.add(word)
        candidates = _candidate_suggestions(word, vocab)
        if candidates:
            hints[token] = list(candidates)
        if len(considered) >= 8:
            break
    return hints


def _repairs(text: str) -> List[Tuple[re.Match, str]]:
    """Find conservative lexical repairs and their exact original spans."""
    text = text or ""
    vocab = _vocabulary()

    found: List[Tuple[re.Match, str]] = []
    for match in _word_matches(text):
        token = match.group(0)
        if token != token.lower():
            continue
        fixed = _suggest(token, vocab)

        if fixed and fixed != token.lower():
            found.append((match, fixed))
            if len(found) >= MAX_CORRECTIONS:
                break
    return found


def misspellings(text: str) -> List[Tuple[str, str]]:
    """Inspect accepted local repairs, not a verdict that other words are correct."""
    return [(match.group(0), meant) for match, meant in _repairs(text)]



def has_misspelling(text: str) -> bool:
    """Whether local repair found a change; False does not prove correct spelling."""
    return bool(misspellings(text))


def correct_query(text: str) -> str:
    """
    `text` with its repairable words replaced, or "" when nothing changed.

    Returns "" rather than unchanged input so callers can keep the original
    question or skip an unnecessary retrieval variant when nothing was fixed.
    """
    t = (text or "").strip()
    if not t:
        return ""

    repairs = _repairs(t)
    if not repairs:
        return ""

    out = t
    # Exact spans, from the right, preserve punctuation and subsequent offsets.
    for match, meant in reversed(repairs):
        out = out[:match.start()] + meant + out[match.end():]

    out = out.strip()
    return "" if out.lower() == t.lower() else out


def spelling_variants(text: str) -> List[str]:
    """
    The retrieval probes to ADD for a possibly-misspelled question.

    An empty result means no confident local repair. Direct/unreviewed callers
    keep the input as their primary query. Validated interpretations must skip
    these variants so rejected local guesses cannot re-enter retrieval.
    """
    corrected = correct_query(text)
    return [corrected] if corrected else []


# --------------------------------------------------------------------------- #
# Growing the vocabulary
# --------------------------------------------------------------------------- #

def learn_terms(texts: Iterable[str], path: str = None) -> int:
    """
    Fold the words of freshly indexed documents into `spell_vocab.json`.

    Called from store_index.py after chunking, so the spell checker's idea of
    "a word the documents contain" is updated by the same action that makes it
    true. Returns the number of terms known afterwards.

    Never raises: an unwritable vocabulary file degrades corrections to the
    terms already known, which is exactly the behaviour before this module
    existed. Losing an indexed document over it would be far worse.
    """
    target = path or SPELL_VOCAB_FILE
    counts = _read_vocab_file(target)

    for text in texts:
        for match in _WORD_RE.finditer(text or ""):
            word = match.group(0).strip("'-").lower()
            if _indexable(word):
                counts[word] = counts.get(word, 0) + 1

    try:
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"terms": dict(sorted(counts.items()))},
                fh,
                ensure_ascii=False,
            )
        os.replace(tmp, target)
    except OSError as e:
        print(f"  Spelling vocabulary not updated (non-fatal): {e}")
        return vocabulary_size()

    return reload_vocabulary()


def _indexable(word: str) -> bool:
    """
    Is this token worth remembering as a real word?

    OCR is the reason this is strict. A scanned page yields "rcgistrar", "thc"
    and "1nformation", and every one of them would otherwise become a legitimate
    target for someone else's typo. Requiring letters, a vowel and a sane length
    throws away most of that noise; MIN_TARGET_FREQ throws away the rest, since
    a garbled word is rarely garbled the same way twice.
    """
    if not word or not word.isalpha():
        return False
    if len(word) < MIN_CORRECTABLE_LEN or len(word) > 24:
        return False
    return any(v in word for v in "aeiouy")
