"""
test_language.py — Tagalog/Waray questions must retrieve English documents.

    python tests/test_language.py

The failure this guards
----------------------
Every document is English; every Samar student types Taglish or Waray. BM25
scores an unseen token at exactly zero and the English MiniLM has no idea that
"pila an bayad" is a fee question, so the phrasing a local student is MOST likely
to use is the one retrieval handles worst — silently, with a confident "I don't
have information about that".

What is actually being checked, therefore, is not "is the translation nice". It
is three things that can each break retrieval on their own:

* the local words become the English words that appear in the documents;
* an English question is left completely alone (a rewrite there is pure noise);
* the student's own wording always survives as the first query, so a term the
  table does not know can never make a question worse than it was.

No network, no model, no credentials: the whole point of a lookup table is that
it can be tested exactly.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import os
import sys

# (The old `sys.path.insert(dirname(__file__))` here is gone: it added the repo
#  root only while this file lived in the repo root. _bootstrap above does it
#  correctly from tests/.)

from rag.language import (          # noqa: E402
    detect_local_terms,
    is_local_language,
    language_variants,
    to_english_query,
)

_passed, _failed = 0, 0


def check(label, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -> {extra}" if extra else ""))


def section(title):
    print(f"\n=== {title} ===")


# ============================================================
section("1. Detecting that a question is not English")
# ============================================================
check("Waray fee question detected", is_local_language("Pila an bayad sa enrollment?"))
check("Tagalog fee question detected", is_local_language("Magkano ang tuition fee?"))
check("Waray who-question detected", is_local_language("Sin-o an dean han education?"))
check("Taglish detected", is_local_language("Kailan ang deadline ng payment?"))

# The other half of the boundary, and the more important one: a false positive
# here rewrites a question that was already working.
check("plain English is left alone",
      not is_local_language("Who is the dean of the College of Education?"))
check("English fee question is left alone",
      not is_local_language("How much is the tuition for BSIT?"))
check("empty string is not 'local'", not is_local_language(""))

# "pila an" must be consumed as one phrase, not reported as "pila" as well —
# otherwise the same word gets translated twice and its BM25 weight doubles.
terms = detect_local_terms("Pila an bayad?")
check("multi-word key wins over its own components",
      "pila an" in terms and "pila" not in terms, terms)


# ============================================================
section("2. The rewrite produces words the documents contain")
# ============================================================
eng = to_english_query("Pila an bayad sa enrollment?")
check("'pila an' -> how much is", "how much" in eng, eng)
check("'bayad' -> payment/fee", "payment" in eng or "fee" in eng, eng)
check("'enrollment' survives", "enrollment" in eng, eng)

eng = to_english_query("Magkano ang tuition?")
check("'magkano' -> how much", "how much" in eng, eng)
check("'tuition' survives", "tuition" in eng, eng)

eng = to_english_query("Sin-o an dean han education?")
check("'sin-o' -> who", eng.startswith("who"), eng)
check("'dean' survives", "dean" in eng, eng)
check("'education' survives", "education" in eng, eng)

eng = to_english_query("Ano an kinahanglan para mag-enroll?")
check("'kinahanglan' -> requirements", "requirements" in eng, eng)

eng = to_english_query("Hain an registrar office?")
check("'hain' -> where", "where" in eng, eng)

eng = to_english_query("Kailan ang pasukan?")
check("'pasukan' -> school year / classes",
      "school year" in eng or "classes" in eng, eng)


# ============================================================
section("3. Particles are dropped, terms are not repeated")
# ============================================================
eng = to_english_query("Pila po ba an bayad, sir?")
for particle in ("po", "ba", "sir", "an"):
    check(f"'{particle}' dropped", particle not in eng.split(), eng)

# "bayad" and "bayaran" both map to "payment fee"; emitting it twice would
# inflate that term's own BM25 weight against the rest of the query.
eng = to_english_query("Pila an bayad ngan bayaran?")
check("duplicate terms collapse", eng.split().count("payment") <= 1, eng)

check("an English question yields no rewrite at all",
      to_english_query("Who is the college dean?") == "")
check("empty input yields nothing", to_english_query("") == "")
check("punctuation-only input yields nothing", to_english_query("???") == "")


# ============================================================
section("4. Variants are additive — the original is never replaced")
# ============================================================
v = language_variants("Pila an bayad sa enrollment?")
check("at least one variant produced", len(v) >= 1, v)
check("the variant is English", "how much" in v[0], v)
check("no variant is empty", all(x.strip() for x in v))

# A short rewrite ("how much payment fee") matches a fee TABLE better when the
# enumerative phrasing is tried too — same trick expand_queries() uses.
v = language_variants("Pila an bayad?")
check("a thin rewrite also gets an enumerative variant",
      any("list of fees" in x for x in v), v)

check("an English question gets no variants",
      language_variants("How much is the tuition fee?") == [])
check("empty question gets no variants", language_variants("") == [])


# ============================================================
section("5. expand_queries() keeps the student's wording first")
# ============================================================
# The integration point. `rag.chain` imports heavy things (Pinecone, the
# embedding model), so the two functions are re-implemented here EXACTLY as they
# compose in chain.py. If that composition changes, this section is the thing
# that should be updated — the ordering guarantee is what it exists to protect.
import re  # noqa: E402

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
THIN_QUERY_TERMS = 4


def _content_terms(query):
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-']+", (query or "").lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def expand_queries(query):
    query = (query or "").strip()
    if not query:
        return []
    queries = [query]
    english = ""
    for variant in language_variants(query):
        if variant not in queries:
            queries.append(variant)
            english = english or variant
    terms = _content_terms(english or query)
    if len(terms) < THIN_QUERY_TERMS and terms:
        core = " ".join(terms)
        for extra in (f"complete list of {core}", f"{core} include the following"):
            if extra not in queries:
                queries.append(extra)
    return queries


q = "Pila an bayad sa enrollment?"
qs = expand_queries(q)
check("the student's exact words are query #1", qs[0] == q, qs[0])
check("an English variant was added", any("how much" in x for x in qs[1:]), qs)
check("no duplicate queries", len(qs) == len(set(qs)), qs)

# Thinness must be judged on the ENGLISH string. Measured on the Waray original
# there are ~0 recognised content words, so every local question would look thin
# and get "complete list of ..." bolted onto a query that is already a rewrite.
qs = expand_queries("Sin-o an dean han College of Education ngan an principal han highschool?")
check("a rich local question does not get list-padding",
      not any(x.startswith("complete list of") for x in qs), qs)

# English behaviour must be byte-for-byte what it was before this feature.
qs = expand_queries("what are the programs offered?")
check("English thin query still gets its paraphrases", len(qs) == 3, qs)
check("and they are the original ones",
      qs[1].startswith("complete list of") and qs[2].endswith("include the following"), qs)
# "who is the dean of the college of education" has exactly 3 content words
# (dean, college, education), so the pre-existing THIN_QUERY_TERMS=4 rule already
# paraphrased it before this feature existed. What must be true is that the
# language layer added NOTHING to it — asserting a bare one-element list would be
# asserting the old thin-query behaviour away.
qs = expand_queries("who is the dean of the college of education")
check("English query gets no language variant",
      not any("payment" in x or "how much" in x for x in qs), qs)
check("English query keeps its original paraphrases only", len(qs) == 3, qs)


check("empty query -> no queries", expand_queries("") == [])


# ============================================================
section("6. Real questions students actually send")
# ============================================================
# Each is asserted to produce the ONE English keyword that decides whether the
# right page can be found at all.
CASES = [
    ("Pila an tuition fee para sa BSIT?",        "tuition"),
    ("Magkano ang bayad sa graduation?",         "payment"),
    ("Sin-o an presidente han Samar College?",   "who"),
    ("Kailan an deadline han payment?",          "deadline"),
    ("Ano an requirements para sa transferee?",  "requirements"),
    ("Hain an cashier office?",                  "where"),
    ("Pwede ba mag-installment han tuition?",    "installment"),
    ("May scholarship ba para sa working student?", "scholarship"),
    ("Paano mag-enroll online?",                 "enroll"),
    ("Pila an multa sa late enrollment?",        "penalty"),
]

for question, must_contain in CASES:
    eng = to_english_query(question)
    check(f"{question[:38]:<38} -> '{must_contain}'", must_contain in eng, eng)


print(f"\n{'='*60}")
print(f"  {_passed} passed, {_failed} failed")
print("=" * 60)

raise SystemExit(1 if _failed else 0)
