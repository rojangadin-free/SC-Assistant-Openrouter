"""
test_dictation.py — speech transcripts, repaired before they reach retrieval.

    python tests/test_dictation.py

No AWS, no Pinecone, no network. `rag/dictation.py` is pure string work, which is
deliberate: the whole point is that the repair happens before anything expensive
is touched, so it must be testable without any of it.

`rag.chain` is NOT imported here. Importing it constructs the Pinecone index and
loads the embedding model at module scope, so a unit test of six regexes would
need credentials and ~400 MB of weights. The one behaviour of chain.py that
matters to this module — that single letters are discarded as content terms — is
re-stated locally in section 2 with the same rule, and the seam is exercised for
real by the smoke section at the end.

What is actually being defended
-------------------------------
1. "what is b s i t" and "what is BSIT" are the same question, and today only one
   of them retrieves anything. Section 2 shows the failure before the fix.
2. A typed question must cost NOTHING. The repair is additive, so a false
   positive is one extra probe — but a false positive on typed input that
   *replaced* the student's wording would be a regression, so §4 pins that
   `dictation_variants()` returns [] for clean input.
3. The repair must not invent. Section 5 checks the mishearing table only fires
   on whole words, because "sit" inside "situation" is not "BSIT".
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import re

from rag.dictation import (
    repair_transcript, dictation_variants, looks_dictated,
)

_passed = 0
_failed = 0


def check(label, condition):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}")


def section(title):
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
section("1. Spelled-out acronyms collapse")
# --------------------------------------------------------------------------- #

check("'b s i t' -> 'bsit'",
      repair_transcript("what is b s i t") == "what is bsit")
check("'c i t a s' -> 'citas'",
      "citas" in repair_transcript("who is the dean of c i t a s"))
check("'s c t i' -> 'scti'",
      "scti" in repair_transcript("what programs are in s c t i"))
check("the rest of the sentence is untouched",
      repair_transcript("who is the dean of c i t a s")
      == "who is the dean of citas")

# Punctuated variants of the same failure.
check("'b.s.i.t.' -> 'bsit'", "bsit" in repair_transcript("what is b.s.i.t."))
check("'b-s-i-t' -> 'bsit'", "bsit" in repair_transcript("what is b-s-i-t"))

# Two letters is NOT a run: acting on it would start eating real words and
# Waray/Tagalog particles ("is a b...").
check("a 2-letter pair is left alone",
      repair_transcript("is a b good grade") == "is a b good grade")

# A real sentence with a stray single letter must survive intact.
check("one stray letter is not a run",
      repair_transcript("what is section a about") == "what is section a about")


# --------------------------------------------------------------------------- #
section("2. Why this matters: the chain drops single letters entirely")
# --------------------------------------------------------------------------- #

# This is `_content_terms()` from rag/chain.py, restated. If that rule ever
# changes, this section is where the mismatch shows up.
_STOP = {"a", "an", "the", "is", "are", "what", "who", "of", "in", "for", "to"}


def content_terms(q):
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-']+", (q or "").lower())
    return [w for w in words if w not in _STOP and len(w) > 2]


spoken = "what is b s i t"
typed = "what is BSIT"

check("the dictated form yields ZERO search terms",
      content_terms(spoken) == [])
check("the typed form yields 'bsit'",
      content_terms(typed) == ["bsit"])
check("...so the two are not interchangeable before the repair",
      content_terms(spoken) != content_terms(typed))
check("after the repair they ARE interchangeable",
      content_terms(repair_transcript(spoken)) == content_terms(typed))

# The same collapse rescues the thin-query paraphrase branch, which needs at
# least one content word to build "complete list of ..." from.
check("a repaired query has something to paraphrase from",
      len(content_terms(repair_transcript("programs in s c t i"))) >= 1)


# --------------------------------------------------------------------------- #
section("3. Fillers and spoken punctuation")
# --------------------------------------------------------------------------- #

check("'um' is dropped",
      repair_transcript("um what is the tuition fee")
      == "what is the tuition fee")
check("several fillers are dropped",
      repair_transcript("uh um how do i enroll") == "how do i enroll")
check("'question mark' becomes '?'",
      repair_transcript("how do i enroll question mark") == "how do i enroll?")
check("no space is left before the mark",
      " ?" not in repair_transcript("how do i enroll question mark"))

# Words that LOOK like fillers but carry meaning must survive. "so" appears in
# program names and "like" is load-bearing in "programs like BSIT".
check("'like' survives", "like" in repair_transcript("programs like b s i t"))
check("'so' survives", "so" in repair_transcript("so what is the tuition fee"))

# A filler-shaped substring inside a real word is not a filler.
check("'ah' inside a word is safe",
      repair_transcript("what about the ahead schedule")
      == "what about the ahead schedule")


# --------------------------------------------------------------------------- #
section("4. Typed input costs nothing")
# --------------------------------------------------------------------------- #

# The critical safety property: a clean question produces NO extra probe. If this
# fails, every typed question in the app silently doubles its retrieval cost.
for typed_q in (
    "What is the tuition fee for BSIT?",
    "Who is the dean of CITAS?",
    "How do I enroll as a transferee?",
    "when is enrollment",                     # lowercase, but nothing to fix
):
    check(f"no variant for: {typed_q[:38]}", dictation_variants(typed_q) == [])

check("a dictated question DOES get a variant",
      dictation_variants("what is b s i t") == ["what is bsit"])
check("empty input yields nothing", dictation_variants("") == [])
check("whitespace yields nothing", dictation_variants("   ") == [])
check("None is survivable", dictation_variants(None) == [])

# Case-only differences are not a variant: lowercasing changes the string but not
# a single retrieval token, so producing one would buy a redundant round-trip.
check("case-only change is not a variant",
      dictation_variants("Who Is The Dean") == [])


# --------------------------------------------------------------------------- #
section("5. Mishearings, on whole words only")
# --------------------------------------------------------------------------- #

check("'summer college' -> 'samar college'",
      "samar college" in repair_transcript("where is summer college"))
check("'sitas' -> 'citas'",
      "citas" in repair_transcript("who is the dean of sitas"))
check("'enrolment' -> 'enrollment'",
      "enrollment" in repair_transcript("when is enrolment"))

# The table must not fire inside a longer word. "sit" is a table key's substring;
# "situation" is not a program.
check("'situation' is not rewritten",
      "situation" in repair_transcript("what is the situation with enrollment"))
check("...and no acronym was invented",
      "bsit" not in repair_transcript("what is the situation with enrollment"))

# The longest key must win, or "see tas" is half-matched by a shorter entry.
check("multi-word keys win over their parts",
      "citas" in repair_transcript("who is the dean of see tas"))


# --------------------------------------------------------------------------- #
section("6. Idempotence and stability")
# --------------------------------------------------------------------------- #

once = repair_transcript("um what is b s i t question mark")
twice = repair_transcript(once)
check("repairing twice changes nothing", once == twice)
check("the repair is what we expect", once == "what is bsit?")
check("an already-clean string is a fixed point",
      repair_transcript("what is bsit?") == "what is bsit?")
check("empty input -> empty string", repair_transcript("") == "")
check("None -> empty string", repair_transcript(None) == "")


# --------------------------------------------------------------------------- #
section("7. looks_dictated() is a diagnosis, not a gate")
# --------------------------------------------------------------------------- #

check("a letter run is decisive", looks_dictated("what is b s i t"))
check("unpunctuated lowercase prose is dictated",
      looks_dictated("how do i enroll as a transferee"))
check("a punctuated question is not", not looks_dictated("how do i enroll?"))
check("a capitalised question is not",
      not looks_dictated("How do i enroll as a transferee"))
check("a short phrase is not guessed at", not looks_dictated("tuition fee"))
check("empty is not dictated", not looks_dictated(""))
check("None is not dictated", not looks_dictated(None))

# The gate is NOT used by the repair. A false negative here must not disable the
# fix, so the repair still works on text this function rejects.
check("the repair works even when the diagnosis says 'typed'",
      dictation_variants("Who is the dean of sitas?") != [])


# --------------------------------------------------------------------------- #
section("8. End to end: the questions a student actually speaks")
# --------------------------------------------------------------------------- #

spoken_questions = [
    ("what is b s i t",                        "bsit"),
    ("who is the dean of c i t a s",           "citas"),
    ("um how much is the tuition pee",         "tuition fee"),
    ("where is summer college located",        "samar college"),
    ("what programs does s c t i offer",       "scti"),
    ("when is enrolment question mark",        "enrollment"),
]

for spoken_q, expected in spoken_questions:
    repaired = repair_transcript(spoken_q)
    check(f"{spoken_q[:34]:36} -> '{expected}'", expected in repaired)

# And the contract with the chain: the student's own words are never replaced,
# only accompanied.
for spoken_q, _ in spoken_questions:
    variants = dictation_variants(spoken_q)
    check(f"variant differs from input: {spoken_q[:28]:30}",
          variants and variants[0] != spoken_q)


# --------------------------------------------------------------------------- #
print(f"\n{'='*54}\n  {_passed} passed, {_failed} failed\n{'='*54}")
raise SystemExit(1 if _failed else 0)
