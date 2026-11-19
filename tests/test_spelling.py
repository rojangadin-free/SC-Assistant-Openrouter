"""
test_spelling.py — a mistyped question must still find the right page.

    python tests/test_spelling.py

The failure this guards
----------------------
"latin honir requirements" is three content words, self-contained, no pronouns —
so every rule in `rag/latency.py` read it as searchable, skipped the LLM rewrite
to save a round-trip, and sent it to retrieval as typed. BM25 then scored `honir`
at zero (no document contains it) and the search silently collapsed to
"latin ... requirements", which fits admission, transfer and graduation
requirements equally badly. The student got a confident, wrong-ish answer and the
content-gap report filed the question as a missing document.

What is checked here is therefore not "is the spelling nice". It is the spelling
and query-rewrite contracts that can each break a question on their own:

* a typo of a word the CORPUS uses is repaired to that word;
* request-opening real-word repairs use sentence context, while genuine losses
  and preferences remain unchanged;
* a correctly spelled question is left completely alone — including Waray,
  dictation and acronyms, which must never be "corrected";
* a repair only ever produces a word the documents actually contain;
* local repairs lead retrieval and generation without changing stored input;
* optional rewrites join retrieval late and cannot force clarification or change
  the natural question, including when the optimizer fails or echoes its input.


No network, no model, no index: vocabulary and edit distance are tested directly,
and the real chat/retrieval code is executed with fake external dependencies.
"""

# Make the repo root importable and force the CWD there.
import _bootstrap  # noqa: F401

import ast           # noqa: E402
import builtins      # noqa: E402
import concurrent.futures  # noqa: E402
import io            # noqa: E402
import json          # noqa: E402
import os            # noqa: E402
import re            # noqa: E402
import tempfile      # noqa: E402
from contextlib import ExitStack  # noqa: E402
from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Optional  # noqa: E402
from unittest.mock import Mock, patch  # noqa: E402

_offline = ExitStack()
_denied_imports, _env_reads = [], []
_original_import = builtins.__import__
_forbidden_imports = (
    "config", "dotenv", "aws", "app", "store_index", "sc_assistant",
    "boto3", "botocore", "pinecone", "pinecone_text", "langchain",
    "langchain_core", "langchain_classic", "langchain_community", "langgraph",
    "langchain_openai", "langchain_huggingface", "sentence_transformers",
    "torch", "transformers", "openai", "rag.chain", "rag.openrouter_reranker",
    "src.helper",
)


def offline_import(name, *args, **kwargs):
    if any(name == prefix or name.startswith(prefix + ".") for prefix in _forbidden_imports):
        _denied_imports.append(name)
        raise AssertionError(f"Live dependency forbidden: {name}")
    return _original_import(name, *args, **kwargs)


def offline_open(opener):
    def guarded(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.path.basename(path).startswith(".env"):
            _env_reads.append(os.fspath(path))
            raise AssertionError("No .env reads")
        return opener(path, *args, **kwargs)
    return guarded


_offline.enter_context(patch("builtins.__import__", new=offline_import))
_offline.enter_context(patch("builtins.open", new=offline_open(builtins.open)))
_offline.enter_context(patch("io.open", new=offline_open(io.open)))
_connections = [_offline.enter_context(patch(target, side_effect=AssertionError("No network")))
                for target in ("socket.create_connection", "socket.socket.connect",
                               "socket.socket.connect_ex", "socket.getaddrinfo")]

# A temp vocabulary, set BEFORE rag.spelling is imported: the module reads the
# path at import time, and the real spell_vocab.json is a build artefact whose
# contents depend on which PDFs happen to be in data/. A suite that asserted
# against it would pass or fail on someone else's corpus.
_temporary = _offline.enter_context(tempfile.TemporaryDirectory(prefix="spelling-test-"))
_VOCAB = os.path.join(_temporary, "vocabulary.json")

# Word counts, not just words: rag/spelling.py refuses to suggest a term it has
# only seen once or twice, because OCR invents words. These are the terms the
# real handbook contains, with plausible frequencies.
with open(_VOCAB, "w", encoding="utf-8") as fh:
    json.dump({"terms": {
        "honor": 31, "honors": 44, "latin": 9, "laude": 12, "graduation": 58,
        "requirements": 96, "enrollment": 120, "enrolment": 14, "tuition": 65,
        "scholarship": 40, "president": 22, "registrar": 35, "dean": 28,
        "college": 210, "education": 88, "transferee": 17, "semester": 74,
        "average": 45, "grades": 52, "subjects": 61, "clearance": 19,
        "samar": 150, "students": 190, "faculty": 46, "curriculum": 23,
        "attendance": 27, "uniform": 15, "guidance": 21, "library": 30,
        # Present so the real-word section below has something to enumerate,
        # and so the short-word traps are reachable: "task" and "part" are
        # exactly one substitution from "talk" and "para".
        "deans": 33, "programs": 70, "task": 4, "tasks": 6, "part": 11,
        "items": 8, "counseling": 26, "receipt": 9, "procedure": 18,
        "history": 20, "moral": 12,
    }}, fh)


os.environ["SC_SPELL_VOCAB"] = _VOCAB

from rag.spelling import (            # noqa: E402
    MIN_CORRECTABLE_LEN,
    correct_query,
    has_misspelling,
    is_known,
    learn_terms,
    misspellings,
    reload_vocabulary,
    spelling_variants,
    suggest,
    vocabulary_size,
)
from rag.latency import Deadline, Timings, pick_search_query, should_optimize  # noqa: E402
from rag.dictation import dictation_variants  # noqa: E402
from rag.language import language_variants  # noqa: E402
from rag import reranker  # noqa: E402
from rag.query_optimizer import (  # noqa: E402
    OPTIMIZER_PROMPT, optimizer_messages, optimize_query, optimize_queries, CORRECTION_PROMPT,
)

reload_vocabulary()

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
section("1. The question that started this")
# ============================================================
# One transposed vowel. Everything else about the question is fine, which is
# exactly why nothing else in the pipeline caught it.
check("'honir' is recognised as a typo of 'honor'",
      suggest("honir") == "honor", suggest("honir"))

check("the whole question is repaired",
      correct_query("latin honir requirements") == "latin honor requirements",
      correct_query("latin honir requirements"))

check("and it is reported as a misspelling",
      has_misspelling("latin honir requirements"))

check("the repair is exposed as a (typed, meant) pair",
      misspellings("latin honir requirements") == [("honir", "honor")],
      misspellings("latin honir requirements"))


# ============================================================
section("1b. Difficult spellings must not be guessed by local repair")
# ============================================================
PHRASE_TYPOS = [
    ("latin hnit requirements", "latin honor requirements"),
    ("latin hnir requirements", "latin honor requirements"),
    ("latn hnir", "latin honor"),
    ("Latin hnit requirements?", "Latin honor requirements?"),
    ("latin hnoir requirements", "latin honor requirements"),
    ("latni honors requirements", "latin honors requirements"),
    ("tuition feez for BSIT", "tuition fees for BSIT"),
    ("tuition feess for BSIT", "tuition fees for BSIT"),
    ("cum ldae requirements", "cum laude requirements"),
    ("good mroal certificate", "good moral certificate"),
    ("hnit policy; latin hnit requirements", "hnit policy; latin honor requirements"),
    ("show 'honir' requirements", "show 'honor' requirements"),
    ("honir- requirements", "honor- requirements"),
]
for typed, meant in PHRASE_TYPOS:
    check(f"a contextual follow-up can request a query rewrite: {typed!r}",
          should_optimize(f"what about {typed}", [{"role": "user", "content": "requirements"}]))
for typed, meant in PHRASE_TYPOS[-2:]:
    check(f"local repair preserves punctuation: {typed!r}", correct_query(typed) == meant)

for question in (
    "hnit requirements", "admission hnit requirements",
    "latin history requirements", "latin hint requirements",
    "latin HNIT requirements", "latin Hnir requirements",
    "latin hzzzz requirements", "latni hnit requirements",
    "latin. hnit requirements", "latin hnit101 requirements",
    "latin 101hnit requirements", "latin honir101 requirements",
    "latin course_hnir requirements",
    "how do i keep a good mood during exams", "good mrna structure",
    "certificaci\u00f3n requirements", "honir\uff11 policy",
    "latin hnit\uff11\uff10\uff11 requirements", "honir\u0301 requirements",
    "latin hnit\u2011101 requirements", "latin hnit\u0301 requirements",
    "latin hnit\u1ab0101 requirements", "is tuition free for BSIT",
):
    check(f"no unsupported phrase guess: {question!r}",
          correct_query(question) == "", correct_query(question))
check("short typos requiring consonant changes are not repaired locally",
      suggest("hnit") is None)
check("an unambiguous missing/mistyped vowel combination is repaired locally",
      suggest("hnir") == "honor" and correct_query("latn hnir") == "latin honor")
check("vowel repair does not replace the surrounding subject",
      correct_query("admission hnir requirements") == "admission honor requirements")
check("a severe typo is not guessed by a special-case local phrase table",
      misspellings("latin hnit requirements") == [])


# ============================================================
section("2. Common ways students mistype campus words")
# ============================================================
# Every target is a word in the vocabulary above, i.e. a word the documents use.
# These are transpositions, doubled letters and dropped letters — the shapes a
# phone keyboard actually produces.
TYPOS = [
    ("requirments",  "requirements"),
    ("enrollmnet",   "enrollment"),
    ("scholarhip",   "scholarship"),
    ("tuiton",       "tuition"),
    ("gradaution",   "graduation"),
    ("registar",     "registrar"),
    ("presidnet",    "president"),
    ("semeter",      "semester"),
    ("atendance",    "attendance"),
    ("libary",       "library"),
]

for typed, meant in TYPOS:
    got = suggest(typed)
    check(f"{typed:<14} -> {meant}", got == meant, got)


# ============================================================
section("3. Correct questions must be left completely alone")
# ============================================================
# This is the half that decides whether the feature is safe to run on every
# message. A false positive here rewrites a working question.
CLEAN = [
    "who is the dean of the college of education",
    "what are the requirements for enrollment",
    "how much is the tuition for this semester",
    "latin honors requirements",
    "is there a scholarship for transferee students",
    "what is the uniform policy",
]

for question in CLEAN:
    check(f"untouched: {question[:44]!r}",
          spelling_variants(question) == [], misspellings(question))

check("a correct question produces no variant",
      correct_query("latin honors requirements") == "")

check("empty input is not a misspelling", has_misspelling("") is False)
check("whitespace input produces no variant", spelling_variants("   ") == [])


# ============================================================
section("4. What must never be 'corrected'")
# ============================================================
# Each of these would be mangled by a general-purpose spell checker, and each is
# a term this assistant depends on.

# Acronyms and dictated letter runs. rag/dictation.py owns those; a spell
# checker guessing at them would fight it.
check("an acronym is not corrected", suggest("bsit") is None, suggest("bsit"))
check("dictated letters are not corrected",
      spelling_variants("what is b s i t") == [],
      spelling_variants("what is b s i t"))

# Waray/Tagalog. rag/language.py translates these; they are not typos, and
# pulling "bayad" toward an English word would destroy the signal it carries.
for word in ("bayad", "pila", "magkano", "kinahanglan"):
    check(f"local-language word kept: {word}", suggest(word) is None, suggest(word))

check("a Waray question produces no spelling variant",
      spelling_variants("Pila an bayad sa enrollment?") == [],
      misspellings("Pila an bayad sa enrollment?"))

# Short words: at three letters or fewer a typo and a real word cannot be told
# apart ("cum" is one edit from sum/cut/cup).
check(f"words shorter than {MIN_CORRECTABLE_LEN} chars are never touched",
      suggest("cum") is None and suggest("shs") is None)

# Ordinary English glue is known even though no document-frequency backs it.
for word in ("please", "about", "there", "would"):
    check(f"common English is known: {word}", is_known(word))

# A word that resembles nothing in the corpus is left as it is: repairing it
# could not change which pages are found, and guessing would invent a query.
check("an unrelated unknown word is left alone",
      suggest("zzyzxqua") is None, suggest("zzyzxqua"))


# ============================================================
section("5. A repair may only produce a word the documents contain")
# ============================================================
# The safety property the whole design rests on. "recieve" is misspelled by any
# dictionary, but nothing in this corpus is close to it, so there is nothing
# useful to search for and no correction is offered.
check("a typo with no corpus neighbour is not repaired",
      suggest("recieve") is None, suggest("recieve"))

for _, meant in TYPOS:
    check(f"target exists in the vocabulary: {meant}", is_known(meant))

# A term seen only once or twice is OCR noise more often than it is a word.
learn_terms(["rcgistrar"], path=_VOCAB)      # seen once
reload_vocabulary()
check("a term seen once is not offered as a correction",
      suggest("rcgistrat") != "rcgistrar", suggest("rcgistrat"))


# ============================================================
section("5b. Real words that are the wrong word here")
# ============================================================
# The second bug, reported after the first fix shipped:
#
#   typed "lost the dens" -> got "lost the deans" -> meant "list the deans"
#
# `dens` was repaired because no document contains it. `lost` was not, because
# it is a perfectly ordinary English word — correct in isolation, wrong in this
# sentence. Only the neighbouring words can tell the difference.
REAL_WORD = [
    ("lost the dens",                  "list the deans"),
    ("lost the deans",                 "list the deans"),
    ("please lost the dens",           "please list the deans"),
    ("lost all programs",              "list all programs"),
    ("lost the requirements",          "list the requirements"),
    ("gice me the deans",              "give me the deans"),
    ("how mich is the tuition",        "how much is the tuition"),
    ("want are the programs",          "what are the programs"),
    ("wgo is the dean",                "who is the dean"),
]

for typed, meant in REAL_WORD:
    check(f"request-opening repair uses sentence evidence: {typed!r}",
          correct_query(typed) == meant, correct_query(typed))
    check(f"thin real-word question can request a query-only rewrite: {typed!r}",
          should_optimize(typed, []))

# And the half that makes it safe. "lost" is a real word students really use,
# and every one of these is a genuine question about a lost thing. A correction
# here would search for a list nobody asked for.
KEEP_LOST = [
    "i lost my id",
    "i lost my receipt",
    "what if i lost my certificate",
    "lost and found office",
    "the lost item procedure",
    "how do i report a lost id",
    "i lost the documents",
    "we lost all receipts",
    "lost all my documents",
]

for question in KEEP_LOST:
    check(f"left alone: {question!r}",
          correct_query(question) == "", correct_query(question))

KEEP_WANT = [
    "i want the list of deans",
    "we want the programs offered",
    "what if i want a scholarship",
]
for question in KEEP_WANT:
    check(f"left alone: {question!r}",
          correct_query(question) == "", correct_query(question))

# The evidence is read from the ORIGINAL words, never from an already-corrected
# copy — otherwise one repair manufactures the context for the next and a
# single typo cascades into a rewritten question.
check("request and noun repairs use the original sentence evidence",
      misspellings("lost the dens") == [("lost", "list"), ("dens", "deans")],
      misspellings("lost the dens"))

# Short real words sit one substitution from unrelated corpus words, which is
# why substitution is only allowed for vowels. Both of these were live false
# positives before that rule existed.
check("'talk' is not turned into 'task'",
      correct_query("who do i talk to about guidance counseling") == "",
      correct_query("who do i talk to about guidance counseling"))
check("Waray 'para' is not turned into 'part'",
      correct_query("ano an requirements para sa transferee") == "",
      correct_query("ano an requirements para sa transferee"))

# A singular/plural difference is a grammar edit, not a spelling repair: BM25
# already matches the two forms, so the probe would be a duplicate AND would
# wrongly tell rag/latency.py the question was unsearchable.
check("an inflection is not treated as a typo",
      suggest("item") is None, suggest("item"))


# ============================================================
section("6. At most a few repairs per message")
# ============================================================

# Five unknown words is not a typo — it is another language, or a paste. Fixing
# them one by one would invent a question nobody asked.
garbage = "xqzt honir enrollmnet requirments tuiton gradaution scholarhip"
check("no more than 3 repairs are applied",
      len(misspellings(garbage)) <= 3, misspellings(garbage))


# ============================================================
section("7. The latency decision")
# ============================================================
# Local spelling repairs do not require a model. Thin questions and actual
# follow-ups can still request a companion rewrite without replacing the input.
check("a misspelled first-turn question can request a rewrite",
      should_optimize("latin honir requirements", []) is True)

check("and mid-conversation too",
      should_optimize("latin honir requirements",
                      [{"role": "user", "content": "hi"}]) is True)

complete_question = "graduate scholarship application eligibility requirements"
check("a complete clean question skips the optional model call",
      should_optimize(complete_question, []) is False)
check("a complete fresh topic also skips mid-conversation",
      should_optimize(complete_question, [{"role": "user", "content": "hi"}]) is False)
check("a thin clean question remains eligible for a rewrite",
      should_optimize("enrollment requirements", []) is True)
check("a reference still requests context even with many content terms",
      should_optimize("what about their graduate scholarship application eligibility requirements",
                      [{"role": "user", "content": "transferees"}]) is True)




# ============================================================
section("8. Learning the corpus vocabulary")
# ============================================================
size_before = vocabulary_size()
learn_terms([
    "The Graduate Studies program offers a Master of Arts in Education.",
    "Graduate Studies applicants submit a transcript and a recommendation.",
    "Graduate Studies applicants attend an interview with the dean.",
], path=_VOCAB)
reload_vocabulary()

check("new document terms are learned", vocabulary_size() > size_before,
      f"{size_before} -> {vocabulary_size()}")
check("a word from the new document is now known", is_known("applicants"))
# "transcript", not "applicants": the seed list already contains "applicant",
# so a typo of the plural sits one edit from BOTH and the repair is genuinely
# ambiguous. What this asserts is the property that matters — a word only this
# document introduced is now repairable, which it was not three lines ago.
check("and a typo of it can now be repaired",
      suggest("transcrpit") == "transcript", suggest("transcrpit"))



# Written atomically, and readable: the app reloads this file at runtime.
with open(_VOCAB, "r", encoding="utf-8") as fh:
    on_disk = json.load(fh)
check("the vocabulary file stays valid JSON with a terms map",
      isinstance(on_disk.get("terms"), dict))
check("no .tmp file is left behind", not os.path.exists(f"{_VOCAB}.tmp"))

# A missing vocabulary must degrade to the baked-in seed terms, never to an
# exception in the answer path.
os.environ["SC_SPELL_VOCAB"] = os.path.join(
    _temporary, "missing-vocabulary.json"
)
import importlib          # noqa: E402
import rag.spelling as _spelling  # noqa: E402

importlib.reload(_spelling)
check("a missing vocabulary file still loads the seed terms",
      _spelling.vocabulary_size() > 0)
check("and the headline repair still works from seeds alone",
      _spelling.suggest("honir") == "honor", _spelling.suggest("honir"))
check("both reported typos are repaired from seeds without a model",
      _spelling.correct_query("latn hnir") == "latin honor")
for typo in ("hnit", "hnir"):
    check(f"missing vocabulary still allows a contextual follow-up rewrite: {typo}",
          should_optimize(f"what about latin {typo} requirements",
                          [{"role": "user", "content": "honors"}]))
check("missing vocabulary is not a reason to guess at a valid word",
      _spelling.correct_query("latin hint requirements") == "")
check("a seed-only repair cannot erase the fee-waiver meaning",
      _spelling.correct_query("is tuition free for BSIT") == "")
for typo in ("hnit", "hnir"):
    noisy_vocab = _spelling._Vocabulary({**_spelling._vocabulary().freq, typo: 1})
    with patch.object(_spelling, "_vocab", noisy_vocab):
        check(f"one OCR occurrence cannot disable a follow-up rewrite: {typo}",
              should_optimize(f"what about latin {typo} requirements",
                              [{"role": "user", "content": "honors"}]))


# ============================================================
section("9. The wiring")
# ============================================================
# The repair is worthless if retrieval never sees it. Asserted against the
# source because exercising expand_queries() for real would construct the
# Pinecone index and load the embedding model.
with open("rag/chain.py", encoding="utf-8") as fh:
    chain_src = fh.read()

check("chain.py adds the corrected phrasing as a retrieval probe",
      "spelling_variants(query)" in chain_src)
check("the expansion helper keeps its input as the first query",
      "queries = [query]" in chain_src)
check("the graph delegates the query-only model call to the shared optimizer",
      "optimize_queries(query_optimizer_model, user_text, history)" in chain_src)
check("the corrected and contextualized queries are passed to retrieval",
      "contextual_query or standalone_query," in chain_src and "user_question=user_text" in chain_src)
check("the graph no longer imports JSON interpretation or offer-resolution APIs",
      all(name not in chain_src for name in (
          "parse_interpretation", "interpretation_guidance", "resolve_offer_acceptance",
      )))

with open("rag/latency.py", encoding="utf-8") as fh:
    latency_src = fh.read()
check("latency gating is separate from the query-only optimizer payload",
      "parse_interpretation" not in latency_src and "optimizer_messages" not in latency_src)

with open("store_index.py", encoding="utf-8") as fh:
    index_src = fh.read()
check("indexing refreshes the corpus vocabulary",
      "_learn_spelling_terms(final_chunks)" in index_src)


# ============================================================
section("10. A repaired chat must take the correctly spelled answer path")
# ============================================================
# Execute the actual graph node and retrieval helpers, not a copy of them.
# Selecting their AST skips module-level Pinecone/model/AWS initialization.
os.environ["SC_SPELL_VOCAB"] = _VOCAB
importlib.reload(_spelling)
tree = ast.parse(chain_src)
check("chat imports the shared correction function", any(
    isinstance(node, ast.ImportFrom) and node.module == "rag.spelling"
    and any(alias.name == "correct_query" for alias in node.names)
    for node in tree.body
))
functions = {
    "_content_terms", "expand_queries", "split_question", "multi_query_retrieve",
    "retrieve_documents", "docs_to_context", "safe_prompt", "create_graph",
}
constants = {
    "_STOPWORDS", "THIN_QUERY_TERMS", "_QUESTION_WORDS", "_DETERMINERS",
    "MIN_PART_TERMS", "MAX_PARTS", "RERANK_CANDIDATES", "FINAL_TOP_K",
}
selected = [node for node in tree.body if (
    isinstance(node, ast.FunctionDef) and node.name in functions
) or (
    isinstance(node, ast.Assign)
    and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets)
)]


class _Graph:
    def __init__(self, state_type):
        self.node = None

    def add_node(self, name, node):
        self.node = node

    def set_entry_point(self, name):
        pass

    def add_edge(self, start, end):
        pass

    def compile(self, **kwargs):
        return self.node


current = SimpleNamespace(
    page_content="CURRENT DIRECTORY: Graduate Studies dean: Dr. Current Dean.",
    metadata={"source": "current-directory.txt", "page": 1, "chunk_id": "current"},
)
stale = SimpleNamespace(
    page_content="ARCHIVED DIRECTORY: Graduate Studies dean: Dr. Previous Dean.",
    metadata={"source": "archived-directory.txt", "page": 1, "chunk_id": "stale"},
)
honors = SimpleNamespace(
    page_content="GRADUATION HONORS AND AWARDS: Latin honors eligibility requirements.",
    metadata={"source": "handbook.txt", "page": 52, "chunk_id": "honors"},
)
admission = SimpleNamespace(
    page_content="ADMISSION REQUIREMENTS: Freshmen submit admission documents.",
    metadata={"source": "handbook.txt", "page": 45, "chunk_id": "admission"},
)
probes, optimizer_prompts, scored_queries = [], [], []
directory_query = "graduate studies deans directory"


def fetch(query):
    probes.append(query)
    if "latin" in query.lower().split():
        return [honors, admission] if "honor" in query.lower() else [admission]
    return [stale] if "lost" in query.split() or "dens" in query.split() else [current]


class _Retriever(SimpleNamespace):
    def weighted_reciprocal_rank(self, results):
        # Both fake leaves return the same order; fusion preserves that order.
        return results[0] if results else []


def filtered_fetch(query, *, filter):
    condition = filter["source"]
    allowed = condition.get("$in", [condition.get("$eq")])
    return [doc for doc in namespace["retriever"].invoke(query)
            if doc.metadata.get("source") in allowed]


def optimize(messages):
    if isinstance(messages[-1], dict):
        if messages[0]["content"] == CORRECTION_PROMPT:
            return SimpleNamespace(content=messages[-1]["content"])
        optimizer_prompts.append(messages)
        latest = messages[-1]["content"].split("\n\nLatest Question: ", 1)[1]
        latest = latest.split("\nLIKELY MISSPELLINGS", 1)[0]
        # This is a deterministic model double, not production correction logic.
        question = dict(PHRASE_TYPOS + REAL_WORD).get(latest, correct_query(latest) or latest)
        if latest == "list the deans":
            question = directory_query
        return SimpleNamespace(content=question)
    return SimpleNamespace(content="Transcribed image text.")


def unavailable_optimizer(messages):
    raise RuntimeError("optimizer unavailable")


def score(pairs, **kwargs):
    scored_queries.extend(query for query, _ in pairs)
    return [10.0 if (
        ("lost" in query.split() and "ARCHIVED" in passage)
        or ("lost" not in query.split() and "CURRENT" in passage)
        or ("honor" in query.lower() and "HONORS" in passage)
        or ("honor" not in query.lower() and "ADMISSION" in passage)
    ) else 0.0 for query, passage in pairs]


from rag.source_policy import SOURCE_SELECTION_GUIDANCE, select_documents

namespace = {
    "os": os, "re": re, "concurrent": concurrent, "datetime": datetime,
    "List": list, "Dict": dict, "Optional": Optional, "ChatState": dict,
    "StateGraph": _Graph, "END": None, "InMemorySaver": lambda: None,
    "HumanMessage": SimpleNamespace, "SystemMessage": SimpleNamespace,
    "correct_query": correct_query,
    "optimize_queries": optimize_queries,
    "spelling_variants": spelling_variants, "dictation_variants": dictation_variants,
    "language_variants": language_variants, "should_optimize": should_optimize,
    "pick_search_query": pick_search_query, "Deadline": Deadline, "Timings": Timings,
    "retriever": _Retriever(invoke=fetch),
    "sparse_retriever": SimpleNamespace(invoke=filtered_fetch),
    "dense_retriever": SimpleNamespace(invoke=filtered_fetch),
    "rerank": Mock(wraps=reranker.rerank), "rerank_multi": Mock(wraps=reranker.rerank_multi),
    "summarizer": SimpleNamespace(invoke=optimize),
    "query_optimizer_model": SimpleNamespace(invoke=optimize),
    "source_priority": lambda *args, **kwargs: {}, "doc_key": lambda source: source,
    "get_doc_dates": lambda: {},
    "get_file_upload_dates": lambda: {},
    "select_documents": select_documents, "SOURCE_SELECTION_GUIDANCE": SOURCE_SELECTION_GUIDANCE,
    "authority_block": lambda *args: "", "freshness_block": lambda *args, **kwargs: "",
    "resolve_role": lambda *args, **kwargs: "student",
    "role_block": lambda role: "",
    "emit_phase": lambda *args, **kwargs: None,
    "system_prompt": "Documents:\n{retrieved_docs}\nHistory:\n{chat_history}",
    "build_citations": lambda docs: [doc.metadata["chunk_id"] for doc in docs],
    "print": lambda *args, **kwargs: None,
}
from rag import runtime  # noqa: E402
from src import prompt as answer_prompts  # noqa: E402
namespace["response_language_prompt"] = answer_prompts.response_language_prompt
for name in ("CapacityError", "RequestBudget", "env_int", "retrieval_pool",
             "preparation_pool", "optimizer_pool"):
    namespace[name] = getattr(runtime, name)
exec(compile(ast.Module(body=selected, type_ignores=[]), "rag/chain.py", "exec"), namespace)
retrieval_spy = Mock(wraps=namespace["retrieve_documents"])
namespace["retrieve_documents"] = retrieval_spy
# A single answer slot makes primary-query contamination impossible to hide
# behind a large context window; production uses the same selection logic.
namespace["FINAL_TOP_K"] = 1
node = namespace["create_graph"]()


def chat_snapshot(question, history=None, image=None, role="student", corrected=None):
    probes.clear()
    optimizer_prompts.clear()
    scored_queries.clear()
    retrieval_spy.reset_mock()
    namespace["rerank"].reset_mock()
    namespace["rerank_multi"].reset_mock()
    state = {"input": question, "chat_history": history or [], "image_data": image,
             "role": role}
    contextualizer = namespace["query_optimizer_model"]

    def staged_invoke(messages):
        if messages[0]["content"] == CORRECTION_PROMPT:
            return SimpleNamespace(content=corrected or messages[-1]["content"])
        return contextualizer.invoke(messages)

    with patch.dict(namespace, {"query_optimizer_model": SimpleNamespace(invoke=staged_invoke)}):
        result = node(state)
    return {
        "messages": [message.content for message in result["messages_to_llm"]],
        "citations": result["citations"], "probes": sorted(probes),
        "optimizer": list(optimizer_prompts), "scores": sorted(set(scored_queries)),
        "primary": retrieval_spy.call_args.args[0],
        "user_question": retrieval_spy.call_args.kwargs["user_question"],
        "ranking_primary": namespace["rerank"].call_args.args[0] if namespace["rerank"].called else None,
    }, state


def answer_path(snapshot):
    # A local repair should have the clean question's answer path, independently
    # of whether an optional model call was necessary.
    return {key: value for key, value in snapshot.items() if key != "optimizer"}


history = [{"role": "user", "content": "what programs are offered?"}]
corrected_grammar = "What are the enrollment requirements for new students?"
contextualized_grammar = "new student enrollment application requirements"
grammar_model = Mock(return_value=SimpleNamespace(content=contextualized_grammar))
with patch.object(reranker, "_load_model", return_value=None), patch.dict(namespace, {
    "query_optimizer_model": SimpleNamespace(invoke=grammar_model),
}):
    grammar, state = chat_snapshot("what is enrollment requirements for new students?",
                                   corrected=corrected_grammar)
    check("grammar correction feeds contextualization before retrieval",
          grammar_model.call_args.args[0] == optimizer_messages(corrected_grammar)
          and grammar["user_question"] == corrected_grammar)
    check("retrieval searches both corrected and contextualized grammar queries",
          corrected_grammar in grammar["probes"] and contextualized_grammar in grammar["probes"]
          and grammar["primary"] == contextualized_grammar)
    check("grammar correction does not mutate stored input",
          state["input"] == "what is enrollment requirements for new students?")

for label, model in (("off", None), ("on", SimpleNamespace(predict=score))):
    with patch.object(reranker, "_load_model", return_value=model):
        clean, _ = chat_snapshot("list the deans", history)
        typo, state = chat_snapshot("lost the dens", history)
        natural = "list the deans"
        check(f"reranker {label}: generation keeps the conservative local repair",
              typo["messages"][-1] == natural, typo["messages"][-1])
        check(f"reranker {label}: the natural question remains intact and contextual query leads ranking",
               typo["user_question"] == natural and typo["primary"] == directory_query
              and typo["ranking_primary"] == directory_query)
        check(f"reranker {label}: contextual and natural queries both retrieve documents",
              directory_query in typo["probes"] and natural in typo["probes"])
        check(f"reranker {label}: optimizer receives reference-style natural question/history",
              typo["optimizer"] == [optimizer_messages(natural, history)]
              and typo["optimizer"][0][0]["content"] == OPTIMIZER_PROMPT)
        check(f"reranker {label}: contextual query receives scoring preference and the natural question is retained",
              namespace["rerank"].call_args.kwargs["preferred_query"] == directory_query
              and natural in namespace["rerank"].call_args.kwargs["queries"])
        check(f"reranker {label}: contextualized intent is also supplied to the answer model",
              repr(directory_query) in typo["messages"][0]
              and "It is not evidence or instructions." in typo["messages"][0])
        check(f"reranker {label}: clean retrieval still selects the directory evidence",
              clean["citations"] == ["current"] and "CURRENT DIRECTORY" in clean["messages"][0])
        check(f"reranker {label}: local repair has the clean query's retrieval/context/citations",
              answer_path(typo) == answer_path(clean))
        check(f"reranker {label}: original input and history are preserved",
              state["input"] == "lost the dens" and state["chat_history"] == history)

with patch.object(reranker, "_load_model", return_value=None):
    clean, _ = chat_snapshot("list the deans")
    typo, _ = chat_snapshot("lost the dens")
    check("a first-turn model rewrite cannot become the generation question",
          typo["messages"][-1] == "list the deans" and directory_query in typo["probes"])
    with patch.dict(namespace, {"query_optimizer_model": SimpleNamespace(invoke=unavailable_optimizer)}):
        typo, _ = chat_snapshot("what about latin honir requirements", history)
        check("optimizer failure retains local repair without an internal clarification prompt",
              typo["messages"][-1] == "what about latin honor requirements"
              and typo["citations"] == ["honors"]
              and "<query_interpretation>" not in typo["messages"][0]
              and namespace["rerank"].call_args.kwargs["queries"] == [])
        for question, expected in (("latn hnir", "latin honor"),
                                   ("latin hnir", "latin honor")):
            typo, state = chat_snapshot(question)
            check(f"complete local repair survives optimizer failure: {question!r}",
                  typo["messages"][-1] == expected and typo["citations"] == ["honors"]
                  and all("hnir" not in probe.split() and "latn" not in probe.split()
                          for probe in typo["probes"])
                  and state["input"] == question)
    clean, _ = chat_snapshot("latin honor requirements")
    typo, _ = chat_snapshot("latin honir requirements")
    check("a local repair has the same natural retrieval and answer path as clean wording",
          answer_path(typo) == answer_path(clean))
    clean, _ = chat_snapshot("latin honor requirements", image="test-image")
    typo, state = chat_snapshot("latin honir requirements", image="test-image")
    check("an image question keeps the local repair, OCR handoff and original image",
          answer_path(typo) == answer_path(clean) and state["image_data"] == "test-image")
    for question in (*KEEP_LOST, *KEEP_WANT, "Pila an bayad sa enrollment?", "what is b s i t"):
        unchanged, _ = chat_snapshot(question)
        check(f"chat keeps non-typos unchanged: {question!r}",
              unchanged["messages"][-1] == question)

    correction_spy = Mock(wraps=correct_query)
    spelling_spy = Mock(wraps=spelling_variants)
    keywords = "latin honor eligibility academic average"
    with patch.dict(namespace, {
        "correct_query": correction_spy,
        "spelling_variants": spelling_spy,
        "query_optimizer_model": SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(content=keywords))),
    }):
        snapshot, state = chat_snapshot("latn hnir", history)
        check("the graph applies correct_query once, before the query-only model call",
              correction_spy.call_count == 1
              and correction_spy.call_args.args == ("latn hnir",))
        check("keyword-only output supplements retrieval without mutating the natural question",
               snapshot["user_question"] == snapshot["messages"][-1] == "latin honor"
               and snapshot["primary"] == keywords and keywords in snapshot["probes"]
              and state["input"] == "latn hnir")
        check("spelling expansion stays enabled when the optimizer succeeds",
              spelling_spy.called and retrieval_spy.call_args.kwargs.get("allow_spelling", True) is True)
        check("the optimizer prompt carries the corrected question, not a JSON payload",
              namespace["query_optimizer_model"].invoke.call_args.args[0]
              == optimizer_messages("latin honor", history))


unit_docs = [
    SimpleNamespace(page_content="CAS_ONLY_RECORD: Arts and Sciences programs.",
                    metadata={"source": "CAS.docx", "chunk_id": "cas",
                              "section_heading": "College of Arts and Sciences"}),
    SimpleNamespace(page_content="CITAS_ONLY_RECORD: Information Technology programs.",
                    metadata={"source": "CITAS.docx", "chunk_id": "citas",
                              "section_heading": "College of Information Technology and Allied Sciences"}),
]


def fetch_units(query):
    probes.append(query)
    return unit_docs


def shorten_to_previous_unit(messages):
    # Reproduce the erroneous model output, not the intended correction logic.
    return SimpleNamespace(content="cas")


cas_history = [{"role": "user", "content": "cas"},
               {"role": "assistant", "content": "CAS_OLD_REPLY: Arts and Sciences."}]
with patch.object(reranker, "_load_model", return_value=None), patch.dict(namespace, {
    "retriever": _Retriever(invoke=fetch_units),
    "query_optimizer_model": SimpleNamespace(invoke=shorten_to_previous_unit),
}):
    for question in ("cas", "citas", "Citas"):
        snapshot, state = chat_snapshot(question, cas_history)
        code = question.lower()
        check(f"a wrong companion query cannot rename the natural unit: {question!r}",
              snapshot["messages"][-1] == question and snapshot["citations"] == [code])
        other = "citas" if code == "cas" else "cas"
        check(f"source selection and answer context stay within the requested unit: {question!r}",
              f"{code.upper()}_ONLY_RECORD" in snapshot["messages"][0]
              and f"{other.upper()}_ONLY_RECORD" not in snapshot["messages"][0]
                and snapshot["user_question"] == question and snapshot["primary"].lower() == "cas"
               and snapshot["ranking_primary"] == "cas")
        check(f"model output can only add a probe, even when it names a previous unit: {question!r}",
              "cas" in snapshot["probes"])
        check(f"the topic switch preserves stored input and history: {question!r}",
              state["input"] == question and state["chat_history"] == cas_history
              and "CAS_OLD_REPLY" not in snapshot["messages"][0])


# Local repairs lead directly to honors. A severe typo can gain honors recall
# through a late query, but the model must never overwrite the user's wording.
from rag import roles  # noqa: E402
admission_history = [
    {"role": "user", "content": "freshman admission requirements"},
    {"role": "assistant", "content": "Freshmen submit admission documents."},
]
with patch.dict(namespace, {
    "resolve_role": roles.resolve_role, "role_block": roles.role_block,
    "system_prompt": answer_prompts.system_prompt,
}):
    for label, model in (("off", None), ("on", SimpleNamespace(predict=score))):
        with patch.object(reranker, "_load_model", return_value=model):
            clean, _ = chat_snapshot("latin honor requirements", admission_history, role="guest")
            for word in ("hnir", "honir"):
                question = f"latin {word} requirements"
                typo, state = chat_snapshot(question, admission_history, role="guest")
                check(f"reranker {label}: local {word} repair takes the clean honors answer path",
                      answer_path(typo) == answer_path(clean) and typo["citations"] == ["honors"])
                check(f"reranker {label}: locally repaired {word} does not reach retrieval or generation",
                      all(word not in q.split() for q in typo["probes"] + typo["scores"])
                      and typo["messages"][-1] == "latin honor requirements")
                check(f"reranker {label}: {word} preserves stored input and guest role",
                      state["input"] == question and state["role"] == "guest"
                      and state["chat_history"] == admission_history)
            with patch.dict(namespace, {
                "FINAL_TOP_K": 2,
                "query_optimizer_model": SimpleNamespace(invoke=lambda messages: SimpleNamespace(content="latin honor requirements")),
            }):
                question = "what about latin hnit requirements"
                typo, state = chat_snapshot(question, admission_history, role="guest")
                check(f"reranker {label}: a severe follow-up typo gains honors recall via a companion",
                      "latin honor requirements" in typo["probes"]
                      and "honors" in typo["citations"] and honors.page_content in typo["messages"][0])
                check(f"reranker {label}: query-only spelling output leaves natural wording in control",
                       typo["user_question"] == typo["messages"][-1] == question
                       and typo["primary"] == "latin honor requirements"
                      and state["input"] == question and state["chat_history"] == admission_history)
            for prior in ([], admission_history):
                question = "how do i become latin honot"
                interpreter = Mock(return_value=SimpleNamespace(content="latin honor requirements"))
                with patch.dict(namespace, {
                    "FINAL_TOP_K": 2,
                    "query_optimizer_model": SimpleNamespace(invoke=interpreter),
                }):
                    typo, state = chat_snapshot(question, prior, role="guest")
                check(f"reranker {label}, history={bool(prior)}: exact honot request is interpreted",
                      interpreter.call_count == 1 and question in interpreter.call_args.args[0][-1]["content"])
                check(f"reranker {label}, history={bool(prior)}: exact honot request retrieves honors evidence",
                      "latin honor requirements" in typo["probes"]
                      and "honors" in typo["citations"] and honors.page_content in typo["messages"][0])
                check(f"reranker {label}, history={bool(prior)}: exact honot request preserves input and history",
                      typo["messages"][-1] == question and state["input"] == question
                      and state["chat_history"] == prior and state["role"] == "guest")

    with patch.object(reranker, "_load_model", return_value=None):
        with patch.dict(namespace, {"query_optimizer_model": SimpleNamespace(invoke=unavailable_optimizer)}):
            snapshot, _ = chat_snapshot("what about latin hnit requirements", admission_history)
            check("a failed follow-up rewrite uses natural retrieval with no forced clarification",
                  snapshot["messages"][-1] == "what about latin hnit requirements"
                  and snapshot["primary"] in snapshot["probes"]
                  and "<query_interpretation>" not in snapshot["messages"][0]
                  and "Ask ONLY one" not in snapshot["messages"][0])
        late = concurrent.futures.Future()
        late.set_running_or_notify_cancel()
        with patch.object(late, "result", side_effect=concurrent.futures.TimeoutError):
            with patch.object(namespace["optimizer_pool"], "submit",
                              return_value=late):
                snapshot, _ = chat_snapshot("what about latin hnir requirements", admission_history)
                late.set_result("LATE_RESULT_MUST_NOT_JOIN")
                check("a late result cannot mutate a response already being prepared",
                      snapshot["messages"][-1] == "what about latin honor requirements"
                      and "LATE_RESULT_MUST_NOT_JOIN" not in snapshot["probes"]
                      and namespace["rerank"].call_args.kwargs["queries"] == []
                      and "<query_interpretation>" not in snapshot["messages"][0])
        phases = []
        with patch.object(namespace["optimizer_pool"], "submit", side_effect=runtime.CapacityError("busy")):
            with patch.dict(namespace, {"emit_phase": lambda callback, phase, **kwargs: phases.append(phase)}):
                snapshot, _ = chat_snapshot("what about latin hnit requirements", admission_history)
                check("capacity refusal keeps natural retrieval and no internal uncertainty guidance",
                      snapshot["messages"][-1] == "what about latin hnit requirements"
                      and snapshot["primary"] in snapshot["probes"]
                      and "<query_interpretation>" not in snapshot["messages"][0])
                check("a busy optimizer never claims it is understanding the question",
                      "understanding" not in phases)

        with patch.dict(namespace, {"query_optimizer_model": SimpleNamespace(invoke=lambda messages: SimpleNamespace(content="requirements for that thing"))}):
            snapshot, _ = chat_snapshot("requirements for that thing")
            check("an ambiguous echoed query still retrieves normally rather than forcing clarification",
                  bool(snapshot["probes"]) and snapshot["messages"][-1] == "requirements for that thing"
                  and namespace["rerank"].call_args.kwargs["queries"] == []
                  and "Ask ONLY one" not in snapshot["messages"][0])
        with patch.dict(namespace, {"query_optimizer_model": SimpleNamespace(invoke=lambda messages: SimpleNamespace(content="not JSON"))}):
            snapshot, _ = chat_snapshot("what about latin hnit requirements", admission_history)
            check("plain-text output needs no JSON schema and is only a companion",
                  "not JSON" in snapshot["probes"]
                  and snapshot["messages"][-1] == "what about latin hnit requirements")
        for output in ("", "   ", "what about latin honor requirements", "WHAT ABOUT LATIN HONOR REQUIREMENTS"):
            with patch.dict(namespace, {"query_optimizer_model": SimpleNamespace(invoke=lambda messages, text=output: SimpleNamespace(content=text))}):
                snapshot, _ = chat_snapshot("what about latin honir requirements", admission_history)
                check(f"blank/echo optimizer output adds no companion: {output!r}",
                      snapshot["primary"] == snapshot["user_question"] == snapshot["messages"][-1]
                      == "what about latin honor requirements"
                      and namespace["rerank"].call_args.kwargs["queries"] == [])


# ============================================================
section("11. Reply-language instructions follow the latest user, not the context")
# ============================================================
# This checks the actual graph's prompt contract, not a simulated LLM's language.
# Do not use the retrieval dictionary as a language detector: English "may" is
# also a Tagalog search term, and translated probes are not the student's words.
local_history = [
    {"role": "user", "content": "Magkano ang tuition?"},
    {"role": "assistant", "content": "Narito ang impormasyon tungkol sa matrikula."},
]
english_history = [
    {"role": "user", "content": "How much is the tuition fee?"},
    {"role": "assistant", "content": "Here are the tuition details."},
]
language_cases = [
    ("How much is the tuition fee?", local_history, None),
    ("May I enroll in May?", local_history, None),
    ("What does Waray mean?", local_history, None),
    ("Magkano ang tuition?", english_history, None),
    ("Pila an bayad sa enrollment?", english_history, None),
    ("How much ang tuition fee?", english_history, None),
    ("Explain it in Tagalog.", english_history, None),
    ("How much is the tuition fee?", local_history, "test-image"),
    ("Pila an bayad sa enrollment?", english_history, "test-image"),
    ("", local_history, "test-image"),
]
notice_doc = SimpleNamespace(
    page_content=("NOTICE: Walang pasok sa November 16, 2026. "
                  "Enrollment extended until November 20, 2026."),
    metadata={"source": "campus-notice.pdf", "page": 2, "chunk_id": "notice"},
)
language_policy = namespace["response_language_prompt"]
with patch.object(reranker, "_load_model", return_value=None):
    with patch.dict(namespace, {
        "system_prompt": answer_prompts.system_prompt,
        "retriever": _Retriever(invoke=lambda query: [notice_doc]),
    }):
        for question, prior, image in language_cases:
            snapshot, state = chat_snapshot(question, prior, image)
            label = f"{question!r} (image={bool(image)})"
            check(f"language policy is last in the system prompt: {label}",
                  bool(language_policy) and snapshot["messages"][0].endswith(language_policy))
            if question:
                check(f"the retrieved notice remains reference material: {label}",
                      notice_doc.page_content in snapshot["messages"][0])
            text = snapshot["messages"][-1]
            if image:
                text = text[0]["text"].split("\n\n[System Note - Image Analysis Provided]:", 1)[0]
            check(f"language source is the user's question, not a translated probe: {label}",
                  len(snapshot["messages"]) == 2 and text == question)
            check(f"language handling does not rewrite stored input/history: {label}",
                  state["input"] == question and state["chat_history"] == prior)


# ============================================================
section("12. Calendar and announcement answers use documents, not manual feeds")
# ============================================================
from rag import freshness  # noqa: E402

check("chat no longer imports manual calendar/announcement providers", not any(
    isinstance(item, ast.ImportFrom) and (
        item.module in ("rag.calendar", "rag.announcements")
        or (item.module == "rag.roles"
            and any(alias.name == "announcement_audience" for alias in item.names))
    ) for item in tree.body
))
check("the prompt has no manual-feed placeholders or authority rules", all(
    marker not in answer_prompts.system_prompt for marker in (
        "{calendar_context}", "{announcements}", "<calendar>", "<announcements>",
        "THE CALENDAR SECTION IS THE AUTHORITY", "ANNOUNCEMENTS BEAT EVERY DOCUMENT",
    )
))

calendar_doc = SimpleNamespace(
    page_content=("ACADEMIC CALENDAR: Enrollment closes November 15, 2026. "
                  "Classes meet on November 16, 2026."),
    metadata={"source": "academic-calendar.pdf", "page": 3, "chunk_id": "calendar"},
)
date_records = {
    "academic-calendar.pdf": {"filename": "academic-calendar.pdf", "effective_date": "2026-06-01"},
    "campus-notice.pdf": {"filename": "campus-notice.pdf", "effective_date": "2026-11-12"},
}
# Assertions must also inspect calls: call_llm catches provider exceptions.
manual_providers = {
    name: Mock(side_effect=AssertionError(f"{name} must not be consulted"))
    for name in ("calendar_block", "announcement_block", "announcement_audience")
}
verified_facts = "<verified_facts>Registrar: Dr. Verified Person.</verified_facts>"
fact_provider = Mock(return_value=verified_facts)
student_record = {"full_name": "Test Student"}
student_context = "STUDENT PERSONAL RECORD\nName: Test Student\nSchedule: PRIVATE_SCHEDULE_0830"
uid_lookup = Mock(return_value=student_record)
email_lookup = Mock(return_value=student_record)
student_formatter = Mock(return_value=student_context)

# Real date/role helpers, with the date snapshot supplied in memory. A missing
# snapshot must fail rather than fall through to a local or live metadata store.
with patch.object(reranker, "_load_model", return_value=None), patch.object(
    freshness, "get_doc_dates", side_effect=AssertionError("Unexpected metadata-store read"),
):
    with patch.dict(namespace, {
        **manual_providers,
        "system_prompt": answer_prompts.system_prompt,
        "retriever": _Retriever(invoke=lambda query: [calendar_doc, notice_doc]),
        "FINAL_TOP_K": 2,
        "get_doc_dates": lambda: date_records,
        "source_priority": freshness.source_priority, "doc_key": freshness.doc_key,
        "freshness_block": freshness.freshness_block, "authority_block": fact_provider,
        "resolve_role": roles.resolve_role, "role_block": roles.role_block,
        "datetime": SimpleNamespace(now=lambda: datetime(2026, 11, 13)),
        "get_student_by_uid": uid_lookup, "get_student_by_email": email_lookup,
        "format_student_context": student_formatter,
    }):
        for label, question, identity, expected_role in (
            ("guest calendar", "When is enrollment?", {"role": "guest"}, "guest"),
            ("student notice", "Are classes suspended?", {"role": "student"}, "student"),
            ("staff calendar", "When is enrollment?", {"role": "registrar"}, "faculty"),
            ("legacy faculty notice", "Are classes suspended?", {"is_faculty": True}, "faculty"),
            ("unlabelled calendar", "When is enrollment?", {}, "student"),
        ):
            result = node({"input": question, "chat_history": [], **identity})
            messages = result["messages_to_llm"]
            assembled = messages[0].content if messages else ""
            check(f"{label}: the accepted question still reaches the answer model",
                  len(messages) == 2 and messages[-1].content == question)
            check(f"{label}: both retrieved documents and their citations survive",
                  all(doc.page_content in assembled
                      and f"Source: {doc.metadata['source']} | Page: {doc.metadata['page']}" in assembled
                      for doc in (calendar_doc, notice_doc))
                  and result["citations"] == ["calendar", "notice"])
            check(f"{label}: dates are labelled without declaring unrelated sources superseded",
                  "[DOCUMENT 1] [EARLIER EFFECTIVE DATE \u2014 effective 2026-06-01]" in assembled
                  and "[DOCUMENT 2] [MOST RECENT DATED SOURCE \u2014 effective 2026-11-12]" in assembled
                  and "<document_dates>\n" in assembled
                  and "campus-notice.pdf \u2014 effective 2026-11-12 (NEWEST)" in assembled
                  and "A NEWER DOCUMENT SUPERSEDES AN OLDER ONE" in assembled)
            check(f"{label}: today's date still grounds the answer",
                  "The current date is November 13, 2026." in assembled)
            check(f"{label}: admin-verified facts still receive the question and evidence",
                  verified_facts in assembled and "ADMIN-VERIFIED FACTS OUTRANK EVERYTHING" in assembled
                  and fact_provider.call_args.args[:2] == (question, question)
                  and all(doc.page_content in fact_provider.call_args.args[2]
                          for doc in (calendar_doc, notice_doc)))
            check(f"{label}: role guidance is unchanged",
                  roles.role_block(expected_role) in assembled)
        check("role alone never fetches a student record",
              not uid_lookup.called and not email_lookup.called and not student_formatter.called)

        for consent in (True, False):
            # Exercise the UID path and its existing email fallback without AWS.
            uid_lookup.return_value = student_record if consent else None
            uid_lookup.reset_mock()
            email_lookup.reset_mock()
            student_formatter.reset_mock()
            result = node({
                "input": "What is my schedule?", "chat_history": [], "role": "student",
                "uid": "test-account-uid", "user_email": "student@example.invalid",
                "data_consent": consent,
            })
            messages = result["messages_to_llm"]
            assembled = messages[0].content if messages else ""
            check(f"consent={consent}: only the account holder's UID is looked up",
                  uid_lookup.call_count == 1 and uid_lookup.call_args.args == ("test-account-uid",))
            if consent:
                check("consenting students retain their formatted personal record",
                      student_context in assembled and student_formatter.call_count == 1
                      and student_formatter.call_args.args == (student_record,)
                      and not email_lookup.called)
            else:
                check("a missing UID record still falls back to the account email",
                      email_lookup.call_count == 1
                      and email_lookup.call_args.args == ("student@example.invalid",))
                check("consent opt-out retains only the basic profile, never the private schedule",
                      "Name: Test Student" in assembled
                      and "explicitly opted out" in assembled
                      and "PRIVATE_SCHEDULE_0830" not in assembled
                      and not student_formatter.called)

        demo_record = {"full_name": "Demo Student 100000", "is_demo": True}
        uid_lookup.return_value = demo_record
        email_lookup.reset_mock()
        student_formatter.reset_mock()
        student_formatter.return_value = "SYNTHETIC DEMO DATA: not proof of enrollment.\nDemo Student 100000\nDEMO_PRIVATE_GRADE_125"
        for consent in (True, False):
            student_formatter.reset_mock()
            result = node({
                "input": "What are my grades?", "chat_history": [], "role": "student",
                "uid": "demo-account-uid", "user_email": "demo@example.invalid", "data_consent": consent,
            })
            assembled = result["messages_to_llm"][0].content
            check(f"demo consent={consent}: synthetic provenance is retained",
                  "SYNTHETIC DEMO DATA" in assembled and "Demo Student 100000" in assembled
                  and "not proof of enrollment" in assembled)
            check(f"demo consent={consent}: only consented grades reach the model",
                  ("DEMO_PRIVATE_GRADE_125" in assembled) == consent
                  and student_formatter.call_count == int(consent)
                  and not email_lookup.called)

        for name, provider in manual_providers.items():
            check(f"{name} is never consulted during prompt assembly", not provider.called)


section("13. Current uploaded documents form a hard evidence boundary")
old_pages = [SimpleNamespace(
    page_content=f"Enrollment OLD_HANDBOOK_RULE_{i}: bring the obsolete checklist.",
    metadata={"source": "Samar-College-2024.pdf", "page": i + 1, "chunk_id": f"old-{i}"},
) for i in range(24)]
new_admission = SimpleNamespace(
    page_content="Enrollment CURRENT_ONLINE_FORM: submit the online application. Other steps are not specified.",
    metadata={"source": "Samar_Colleges_Inc_Admission_Policy.docx", "page": 1,
              "section": "Admission Policy", "chunk_id": "current-admission"},
)
new_registrar = SimpleNamespace(
    page_content="Enrollment CURRENT_REGISTRAR_REVIEW: the registrar checks the submitted online form.",
    metadata={"source": "REGISTRAR_Services__Information.docx", "page": 1,
              "section": "Enrollment services", "chunk_id": "current-registrar"},
)
source_dates = {"samar-college-2024.pdf": {"effective_date": "2024-06-01"}}
upload_dates = {
    new_admission.metadata["source"]: "2026-11-13T02:39:28+00:00",
    new_registrar.metadata["source"]: "2026-11-13T02:39:24+00:00",
}
catalog_reader = Mock(return_value=upload_dates)
summary_spy = Mock(return_value="OLD_SUMMARY_REINTRODUCED")
ranked_sizes = []


def unchanged_ranking(query, docs, **kwargs):
    ranked_sizes.append(kwargs["top_k"])
    return list(docs)[:kwargs["top_k"]]


history_with_old_facts = [{"role": "system", "content": "OLD_STORED_SUMMARY"}]
for i in range(6):
    history_with_old_facts.extend([
        {"role": "user", "content": f"Earlier user question {i}"},
        {"role": "assistant", "content": f"OLD_ASSISTANT_FACT_{i} [SOURCE:Samar-College-2024.pdf|p.1]"},
    ])
original_history = [dict(message) for message in history_with_old_facts]
with patch.dict(os.environ, {"RAG_SOURCE_FILTER_ENABLED": "true"}), patch.dict(namespace, {
    "system_prompt": answer_prompts.system_prompt,
    "retriever": _Retriever(invoke=lambda query: [*old_pages, new_admission, new_registrar]),
    "rerank": unchanged_ranking, "rerank_multi": unchanged_ranking,
    "FINAL_TOP_K": 2, "get_doc_dates": lambda: source_dates,
    "get_file_upload_dates": catalog_reader,
    "source_priority": freshness.source_priority, "doc_key": freshness.doc_key,
    "freshness_block": freshness.freshness_block,
    "resolve_role": roles.resolve_role, "role_block": roles.role_block,
    "summarize_history": summary_spy,
    "datetime": SimpleNamespace(now=lambda: datetime(2026, 11, 14)),
}):
    for role in ("guest", "student", "faculty"):
        result = node({"input": "What is the enrollment procedure?", "role": role,
                       "chat_history": history_with_old_facts})
        assembled = result["messages_to_llm"][0].content if result["messages_to_llm"] else ""
        check(f"{role}: update outside the old top-K cutoff reaches the answer prompt",
              "CURRENT_ONLINE_FORM" in assembled and "CURRENT_REGISTRAR_REVIEW" in assembled)
        check(f"{role}: no handbook body or filename reaches the answer prompt",
              "OLD_HANDBOOK_RULE" not in assembled and "Samar-College-2024.pdf" not in assembled)
        check(f"{role}: only selected current sources become citations",
              result["citations"] == ["current-admission", "current-registrar"])
        check(f"{role}: old assistant facts and summaries cannot refill omitted details",
              "OLD_ASSISTANT_FACT" not in assembled and "OLD_STORED_SUMMARY" not in assembled
              and "OLD_SUMMARY_REINTRODUCED" not in assembled and not summary_spy.called)
        check(f"{role}: user intent and stored conversation remain intact",
              "Earlier user question 5" in assembled and result["chat_history"] == original_history
              and history_with_old_facts == original_history)
        check(f"{role}: current-source and no-old-program instructions are explicit",
              SOURCE_SELECTION_GUIDANCE in assembled and "CURRENT CATALOG BOUNDARY" in assembled
              and "ORPHANED PROGRAMS OVERRIDE" not in assembled and "BA Sociology" not in assembled)
    check("the graph keeps the bounded candidate pool until source selection",
          ranked_sizes and all(size == namespace["RERANK_CANDIDATES"] for size in ranked_sizes))
    check("upload metadata is read once per answer, not once per document", catalog_reader.call_count == 3)

    with patch.dict(namespace, {"retriever": _Retriever(invoke=lambda query: [old_pages[0], new_admission])}):
        historical = node({"input": "Compare the 2024 and 2026 enrollment procedures.", "role": "guest", "chat_history": []})
        text = historical["messages_to_llm"][0].content if historical["messages_to_llm"] else ""
        check("explicit version comparisons retain both requested source versions",
              "OLD_HANDBOOK_RULE_0" in text and "CURRENT_ONLINE_FORM" in text
              and historical["citations"] == ["old-0", "current-admission"])
        check("historical comparisons do not receive a current-only source lock", SOURCE_SELECTION_GUIDANCE not in text)

    unrelated = SimpleNamespace(
        page_content="Latin honors requirements use the documented grade average.",
        metadata={"source": "Honors-Notice.docx", "page": 1, "chunk_id": "honors-notice"},
    )
    with patch.dict(namespace, {
        "retriever": _Retriever(invoke=lambda query: [old_pages[0], unrelated]),
        "get_file_upload_dates": lambda: {"Honors-Notice.docx": "2026-11-13T03:00:00Z"},
    }):
        fallback = node({"input": "What is the enrollment procedure?", "role": "guest", "chat_history": []})
        text = fallback["messages_to_llm"][0].content if fallback["messages_to_llm"] else ""
        check("an unrelated new upload leaves handbook fallback available", "OLD_HANDBOOK_RULE_0" in text)
        check("an unrelated upload does not falsely claim current-source coverage", SOURCE_SELECTION_GUIDANCE not in text)

    with patch.dict(namespace, {
        "get_doc_dates": lambda: {**source_dates, new_admission.metadata["source"].lower(): {"effective_date": "2026-06-01"}},
        "get_file_upload_dates": Mock(side_effect=RuntimeError("catalog unavailable")),
    }):
        fallback_dates = node({"input": "What is the enrollment procedure?", "role": "guest", "chat_history": []})
        text = fallback_dates["messages_to_llm"][0].content if fallback_dates["messages_to_llm"] else ""
        check("valid effective dates still enforce precedence if upload metadata is unavailable",
              "CURRENT_ONLINE_FORM" in text and "OLD_HANDBOOK_RULE" not in text
              and fallback_dates["citations"] == ["current-admission"])

section("14. Offline isolation")
check("no live application/model/AWS imports were attempted", _denied_imports == [], _denied_imports)
check("no .env file was read", _env_reads == [], _env_reads)
check("no network connection was attempted", all(not connection.called for connection in _connections))
_offline.close()

print(f"\n{'='*60}")
print(f"  {_passed} passed, {_failed} failed")
print("=" * 60)

raise SystemExit(1 if _failed else 0)
