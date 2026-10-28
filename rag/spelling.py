"""
rag/spelling.py — a misspelled question must still find the right page.

The failure this fixes
----------------------
A student types:

    "latin honir requirements"

and gets a vague, wrong-ish answer. Nothing in the pipeline was broken; every
layer did exactly what it was told:

* BM25 scores `honir` at **zero**. The token appears in no chunk of the
  handbook, so the strongest retrieval signal in the question contributes
  nothing, and the query effectively degrades to "latin ... requirements" —
  which matches admission requirements, transfer requirements, graduation
  requirements and a dozen other pages equally well.
* The dense encoder embeds a nonsense token somewhere unhelpful, blurring the
  vector away from the "GRADUATION HONORS AND AWARDS" section it should be
  nearest to.
* `should_optimize()` in rag/latency.py measured the question as *searchable*:
  three content words (`latin`, `honir`, `requirements`), no pronouns, no
  follow-up markers — so the LLM rewrite that would have silently fixed the
  typo was skipped, on purpose, to save a round-trip.

Every one of those decisions is individually correct. Together they mean one
transposed letter quietly costs the student the right answer, and the
content-gap report then files the question as a missing document — the same
pollution rag/dictation.py was written to stop.

Why a dictionary of the CORPUS, not a dictionary of English
-----------------------------------------------------------
A general spell checker is the wrong tool twice over. It does not know
`BSIT`, `CITAS`, `SCTI`, `Waray` or `laude`, so it would "fix" the exact terms
that matter most, and it does not know which of its suggestions exists in
*these* documents — the only suggestions worth making.

So the vocabulary is the corpus itself: every word the indexed documents
actually contain, with how often it appears (`spell_vocab.json`, written by
`tools/build_spell_vocab.py` and refreshed whenever a document is indexed).
A token is only ever replaced by a word the retriever can actually match, and
frequency breaks ties toward the word the documents talk about most.

A small seed list is baked in below so the module still works on a fresh
checkout that has never built the vocabulary file, and so that vocabulary
gathered from a scanned page (OCR produces its own garbage words) can never
outvote the terms we know are real.

Rules of engagement
-------------------
* **One working question in chat.** The graph applies accepted repairs before
  optimization, retrieval, reranking and generation. Merely adding a repaired
  probe leaves the typo in control of candidate ordering and answer intent.
  The variant helper remains additive for callers that use it directly.
* **Conservative.** Short words are left alone, the first letter must match,
  the edit distance is bounded, and an ambiguous repair (two equally good
  corpus words) is declined rather than guessed.
* **Nothing is rewritten on screen.** The student sees what they typed; only
  the working question is repaired. The original input and history are kept.

The one thing this module decides beyond retrieval is `has_misspelling()`,
which rag/latency.py uses to answer "is this question searchable as typed?".
A still-misspelled working question may need the optimizer. A locally repaired
question takes the same latency decision as its correctly spelled counterpart.
"""

from __future__ import annotations

import json
import os
import re
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

# At most this many repairs per question. A message where five words are unknown
# is not a typo, it is another language (rag/language.py) or a paste of garbage,
# and "correcting" it word by word would invent a query nobody asked.
MAX_CORRECTIONS = 3


# --------------------------------------------------------------------------- #
# Real-word typos
# --------------------------------------------------------------------------- #
#
# Everything above this point can only repair a word that is in NO dictionary:
# `honir`, `requirments`, `enrolmnet`. That misses the harder half of the
# problem, and a student found it immediately:
#
#     typed:      "lost the dens"
#     repaired:   "lost the deans"        <- only half fixed
#     meant:      "list the deans"
#
# `dens` is unknown, so it was repaired. `lost` is a perfectly ordinary English
# word, so `is_known()` answered True and `_suggest()` returned None without
# even looking — which is correct in isolation and wrong in this sentence. A
# spell checker cannot see the difference; only the surrounding words can.
#
# So a *known* word is only ever reconsidered when the sentence itself says it
# does not belong, and then only for a tiny, explicit set of confusions.
#
# Two pieces of evidence are required, which is what keeps "i lost my id" safe:
#
#   1. The typed word must be one of the pairs below — a confusion that is one
#      keystroke wide and changes what is being ASKED, not merely which word was
#      used. This is a table, not a distance search: opening real words up to
#      edit-distance matching would let "dean" become "dear", "mean", "dead"...
#   2. The sentence must supply the context the replacement needs. "lost" only
#      becomes "list" when a plural noun follows it ("lost the deans"), because
#      that is the shape of a request for an enumeration and NOT the shape of
#      "i lost my id" or "lost and found".
#
# Fails closed. When the context is absent the student's word stands, and the
# probe is simply not added.

# typed -> intended, for confusions where the two words ask different questions.
_REAL_WORD_CONFUSIONS: Dict[str, str] = {
    # The reported case. A dropped/slipped vowel on the keyboard's home row,
    # and the single most common way a student asks for an enumeration.
    "lost": "list",
    "lsit": "list",
    "liat": "list",
    # "gice me the deans", "gove me the requirements"
    "gice": "give",
    "gove": "give",
    # "how mich is the tuition", "how muxh"
    "mich": "much",
    "muxh": "much",
    # "want are the programs"
    "want": "what",
    "waht": "what",
    "hwat": "what",
    # "who si the dean"
    "si": "is",
    # "wgo is the dean"
    "wgo": "who",
    "hwo": "who",
    # "whne is enrollment"
    "whne": "when",
    "wehn": "when",
}

# A real-word repair is only applied when the word AFTER it looks like this.
# Keyed by the intended word, because the evidence differs per confusion:
#
#   "list"  needs something enumerable after it  -> "list the deans"
#   "what"  needs a verb/determiner              -> "what are the programs"
#
# `None` means "no following-word evidence required" — used for confusions that
# are not real words in the first place ("lsit", "waht"), where the table is
# only supplying a better answer than edit distance would.
_CONTEXT_AFTER: Dict[str, Optional[Tuple[str, ...]]] = {
    "list": ("the", "all", "every", "down", "of"),
    "give": ("me", "us", "the", "a"),
    "much": ("is", "are", "does", "do", "the", "for"),
    "what": ("is", "are", "was", "were", "do", "does", "the", "a", "an"),
    "is": ("the", "a", "an"),
    "who": ("is", "are", "was", "were", "the"),
    "when": ("is", "are", "does", "do", "will", "the"),
}



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

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


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

    Built once per process and rebuilt on demand (`reload()`), because indexing
    a new document adds terms to the file while the app is running.
    """

    def __init__(self, terms: Dict[str, int]):
        self.freq: Dict[str, int] = dict(terms)

        # Candidates are only ever compared against words with the same first
        # letter and a similar length, so the search space is bucketed by
        # exactly that. Without it every unknown token would be scored against
        # the whole corpus vocabulary — thousands of edit-distance calls on the
        # request path.
        self.buckets: Dict[Tuple[str, int], List[str]] = {}
        for word in self.freq:
            self.buckets.setdefault((word[0], len(word)), []).append(word)

    def candidates(self, word: str, max_edits: int) -> Iterable[str]:
        head = word[0]
        for length in range(len(word) - max_edits, len(word) + max_edits + 1):
            for candidate in self.buckets.get((head, length), ()):
                yield candidate


_vocab: Optional[_Vocabulary] = None


def _vocabulary() -> _Vocabulary:
    global _vocab
    if _vocab is None:
        terms = {w: SEED_FREQ for w in _SEED_TERMS}
        for word, count in _read_vocab_file(SPELL_VOCAB_FILE).items():
            # The corpus wins when it has actually seen the word more often than
            # the seed floor; a seed term absent from the documents keeps its
            # floor so it stays usable.
            terms[word] = max(count, terms.get(word, 0))
        _vocab = _Vocabulary(terms)
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
    """True when the word needs no repair: the documents use it, or it is
    ordinary English glue."""
    w = (word or "").strip().lower()
    if not w:
        return True
    return w in _COMMON_ENGLISH or w in _vocabulary().freq


@lru_cache(maxsize=4096)
def _suggest(word: str) -> Optional[str]:
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

    if len(w) < MIN_CORRECTABLE_LEN or not w.isalpha():
        return None
    if is_known(w):
        return None

    vocab = _vocabulary()
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
    return _suggest((word or "").strip())


def _plural_ish(word: str) -> bool:
    """
    A rough "is this a countable thing, plural" test, used as the evidence that
    a list was being asked for.

    Deliberately crude: request position and possessive guards are checked
    separately. `-ss` is excluded so "process" and "class" are not plurals.
    """
    w = (word or "").lower().strip("?.,!")
    return len(w) > 3 and w.endswith("s") and not w.endswith("ss")


def _real_word_repair(
    token: str, following: List[str], preceding: List[str]
) -> Optional[str]:
    """
    The repair for a token that IS a real word but is the wrong one here.

    `following` and `preceding` are the original surrounding words, lowercased.
    Returns None unless the request supplies the needed evidence:

      1. `token` is in `_REAL_WORD_CONFUSIONS`;
      2. a real word appears at the request opening (optionally after politeness);
      3. the next word is expected, and "list" has a non-personal plural object.

    Worked example, the question that prompted this:

        "lost the dens"   -> dens is unknown  -> "deans"     (the old path)
                          -> lost is known    -> "list"      (this path:
                                                  "the" follows, and "deans"
                                                  is a plural noun)
        => "list the deans"

    And the case it must NOT touch:

        "i lost my id"    -> "my" is not an expected follower, and there is no
                             plural noun          => no repair
    """
    typed = (token or "").lower()
    meant = _REAL_WORD_CONFUSIONS.get(typed)
    if not meant or meant == typed:
        return None

    # A confusion that is not itself a word ("lsit", "waht") needs no context:
    # nothing is being overruled, and edit distance would only guess worse.
    #
    # `_COMMON_ENGLISH` rather than `is_known()`: whether the CORPUS happens to
    # contain "lost" is irrelevant here — it is an English word either way, and
    # "i lost my id" must survive on a corpus that never mentions losing
    # anything. Using is_known() made that depend on the documents, which is
    # how the test suite caught it.
    if typed not in _COMMON_ENGLISH:
        return meant

    # "I lost the documents" and "I want the programs" describe real actions,
    # not misspelled requests. Do not reinterpret words inside a sentence.
    if any(word not in ("please", "kindly") for word in preceding):
        return None

    expected = _CONTEXT_AFTER.get(meant)
    if not expected:
        return None

    next_word = following[0].strip("?.,!") if following else ""
    if next_word not in expected:
        return None

    # "lost all my documents" is still a loss, even though it names a plural.
    if meant == "list":
        if any(word in ("my", "our", "your", "his", "her", "their")
               for word in following[:2]):
            return None
        if not any(_plural_ish(w) for w in following):
            return None

    return meant


def misspellings(text: str) -> List[Tuple[str, str]]:
    """
    The repairs this text needs, as `(typed, meant)` pairs, in order.

    Two kinds, in priority order per token:

      1. a real-word confusion the sentence contradicts ("lost the deans"),
         which needs the neighbouring words as evidence;
      2. a token no dictionary and no document contains ("honir"), which is
         repaired by edit distance against the corpus.

    Exported so the decision is inspectable: a silent correction is very hard to
    debug when retrieval later goes strange, and rag/latency.py needs to know
    whether there was one at all.
    """
    tokens = [m.group(0) for m in _WORD_RE.finditer(text or "")]
    lowered = [t.lower() for t in tokens]

    found: List[Tuple[str, str]] = []
    for i, token in enumerate(tokens):
        # The real-word check runs FIRST and on the ORIGINAL words. Running it
        # on an already-corrected copy would let one repair create the context
        # for the next, which is how a single typo turns into a rewritten
        # question.
        fixed = _real_word_repair(token, lowered[i + 1:], lowered[:i])
        if not fixed:
            fixed = _suggest(token.strip("'-"))

        if fixed and fixed != token.lower():
            found.append((token, fixed))
            if len(found) >= MAX_CORRECTIONS:
                break
    return found



def has_misspelling(text: str) -> bool:
    """
    True when the question carries a word no document contains.

    This is the retrieval-relevant definition of a typo, not the dictionary one:
    "recieve" is misspelled and this returns False if nothing in the corpus is
    close to it, because no repair would change which pages are found. What it
    catches is the case that costs an answer — a word that WOULD have matched a
    page if it had been typed correctly.
    """
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

    repairs = misspellings(t)
    if not repairs:
        return ""

    out = t
    for typed, meant in repairs:
        out = re.sub(rf"\b{re.escape(typed)}\b", meant, out, count=1)

    out = out.strip()
    return "" if out.lower() == t.lower() else out


def spelling_variants(text: str) -> List[str]:
    """
    The retrieval probes to ADD for a possibly-misspelled question.

    `[]` for a correctly spelled one, which is the overwhelmingly common case
    and the reason this is safe to run on every question. The caller keeps the
    input as the primary query. Chat repairs its working input before expansion.
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
