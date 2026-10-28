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

What is checked here is therefore not "is the spelling nice". It is the five
things that can each break a question on their own:

* a typo of a word the CORPUS uses is repaired to that word;
* a REAL word that is the wrong word here is repaired using the sentence as
  evidence ("lost the dens" -> "list the deans"), and only then;
* a correctly spelled question is left completely alone — including Waray,
  dictation and acronyms, which must never be "corrected";
* a repair only ever produces a word the documents actually contain;
* a misspelled question is no longer treated as searchable-as-typed, so the
  rewrite it needs is not skipped.


No network, no model, no index: vocabulary and edit distance are tested directly,
and the real chat/retrieval code is executed with fake external dependencies.
"""

# Make the repo root importable and force the CWD there.
import _bootstrap  # noqa: F401

import ast           # noqa: E402
import concurrent.futures  # noqa: E402
import json          # noqa: E402
import os            # noqa: E402
import re            # noqa: E402
import tempfile      # noqa: E402
from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Optional  # noqa: E402
from unittest.mock import patch  # noqa: E402

# A temp vocabulary, set BEFORE rag.spelling is imported: the module reads the
# path at import time, and the real spell_vocab.json is a build artefact whose
# contents depend on which PDFs happen to be in data/. A suite that asserted
# against it would pass or fail on someone else's corpus.
_VOCAB = os.path.join(tempfile.gettempdir(), ".test_spell_vocab.json")

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
    got = correct_query(typed)
    check(f"{typed:<28} -> {meant}", got == meant, got)

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
check("a repair does not create the context for another",
      len(misspellings("lost the dens")) == 2,
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
# The second half of the fix. Retrieval probes the corrected text locally, but
# the LLM rewrite is what can use the CONVERSATION to disambiguate — and it was
# being skipped precisely on the questions that needed it.
check("a misspelled first-turn question now waits for the rewrite",
      should_optimize("latin honir requirements", []) is True)

check("and mid-conversation too",
      should_optimize("latin honir requirements",
                      [{"role": "user", "content": "hi"}]) is True)

# The skip path must survive: that is the latency work this must not undo.
# Both of these carry three content words, which is what makes them searchable
# as typed — "requirements for enrollment" has only two, so it is thin and would
# wait for its own (pre-existing) reason.
check("a correctly spelled self-contained question still skips it",
      should_optimize("who is the dean of the college of education", []) is False)
check("a correct question skips it mid-conversation as well",
      should_optimize("who is the dean of the college of education",
                      [{"role": "user", "content": "hi"}]) is False)
# Not "how much is the tuition for THIS semester": `this` is a deictic, so that
# question waits for a pre-existing reason of its own (rag/latency.py rule 2b)
# and would prove nothing about spelling either way.
check("a correct fee question still skips it",
      should_optimize("how much is the tuition fee for bsit",
                      [{"role": "user", "content": "hi"}]) is False)




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
    tempfile.gettempdir(), ".test_spell_vocab_absent.json"
)
import importlib          # noqa: E402
import rag.spelling as _spelling  # noqa: E402

importlib.reload(_spelling)
check("a missing vocabulary file still loads the seed terms",
      _spelling.vocabulary_size() > 0)
check("and the headline repair still works from seeds alone",
      _spelling.suggest("honir") == "honor", _spelling.suggest("honir"))


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
check("the optimizer prompt is told about the misspellings",
      "misspellings(user_text)" in chain_src)
check("and it has an explicit spelling rule",
      "2. SPELLING:" in chain_src)

with open("rag/latency.py", encoding="utf-8") as fh:
    latency_src = fh.read()
check("should_optimize() consults the spell checker",
      "has_misspelling(q)" in latency_src)

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
probes, optimizer_prompts, scored_queries = [], [], []


def fetch(query):
    probes.append(query)
    return [stale] if "lost" in query.split() or "dens" in query.split() else [current]


def optimize(messages):
    if isinstance(messages[-1], dict):
        optimizer_prompts.append(messages)
        latest = messages[-1]["content"].split("Latest Question: ", 1)[1]
        # Echoing is supported; the local repair must not depend on an LLM
        # obeying a spelling hint or even returning within its time budget.
        return SimpleNamespace(content=latest.split("\nLIKELY MISSPELLINGS", 1)[0])
    return SimpleNamespace(content="Transcribed image text.")


def unavailable_optimizer(messages):
    raise RuntimeError("optimizer unavailable")


def score(pairs, **kwargs):
    scored_queries.extend(query for query, _ in pairs)
    return [10.0 if (
        ("lost" in query.split() and "ARCHIVED" in passage)
        or ("lost" not in query.split() and "CURRENT" in passage)
    ) else 0.0 for query, passage in pairs]


namespace = {
    "re": re, "concurrent": concurrent, "datetime": datetime,
    "List": list, "Dict": dict, "Optional": Optional, "ChatState": dict,
    "StateGraph": _Graph, "END": None, "InMemorySaver": lambda: None,
    "HumanMessage": SimpleNamespace, "SystemMessage": SimpleNamespace,
    "correct_query": correct_query, "misspellings": misspellings,
    "spelling_variants": spelling_variants, "dictation_variants": dictation_variants,
    "language_variants": language_variants, "should_optimize": should_optimize,
    "pick_search_query": pick_search_query, "Deadline": Deadline, "Timings": Timings,
    "retriever": SimpleNamespace(invoke=fetch),
    "rerank": reranker.rerank, "rerank_multi": reranker.rerank_multi,
    "summarizer": SimpleNamespace(invoke=optimize),
    "source_priority": lambda *args: {}, "doc_key": lambda source: source,
    "authority_block": lambda *args: "", "freshness_block": lambda *args: "",
    "calendar_block": lambda *args: "", "announcement_block": lambda **kwargs: "",
    "resolve_role": lambda *args, **kwargs: "student",
    "announcement_audience": lambda role: "students", "role_block": lambda role: "",
    "emit_phase": lambda *args, **kwargs: None,
    "system_prompt": "Documents:\n{retrieved_docs}\nHistory:\n{chat_history}",
    "build_citations": lambda docs: [doc.metadata["chunk_id"] for doc in docs],
    "print": lambda *args, **kwargs: None,
}
exec(compile(ast.Module(body=selected, type_ignores=[]), "rag/chain.py", "exec"), namespace)
# A single answer slot makes primary-query contamination impossible to hide
# behind a large context window; production uses the same selection logic.
namespace["FINAL_TOP_K"] = 1
node = namespace["create_graph"]()


def chat_snapshot(question, history=None, image=None):
    probes.clear()
    optimizer_prompts.clear()
    scored_queries.clear()
    state = {"input": question, "chat_history": history or [], "image_data": image}
    result = node(state)
    return {
        "messages": [message.content for message in result["messages_to_llm"]],
        "citations": result["citations"], "probes": sorted(probes),
        "optimizer": list(optimizer_prompts), "scores": sorted(set(scored_queries)),
    }, state


history = [{"role": "user", "content": "what programs are offered?"}]
for label, model in (("off", None), ("on", SimpleNamespace(predict=score))):
    with patch.object(reranker, "_load_model", return_value=model):
        clean, _ = chat_snapshot("list the deans", history)
        typo, state = chat_snapshot("lost the dens", history)
        check(f"reranker {label}: answer model sees 'list the deans'",
              typo["messages"][-1] == "list the deans", typo["messages"][-1])
        check(f"reranker {label}: identical retrieval probes",
              typo["probes"] == clean["probes"], typo["probes"])
        check(f"reranker {label}: identical optimizer input",
              typo["optimizer"] == clean["optimizer"])
        check(f"reranker {label}: identical ranking queries",
              typo["scores"] == clean["scores"], typo["scores"])
        check(f"reranker {label}: identical context and selected evidence",
              typo["messages"][0] == clean["messages"][0]
              and typo["citations"] == clean["citations"] == ["current"])
        check(f"reranker {label}: original input and history are preserved",
              state["input"] == "lost the dens" and state["chat_history"] == history)

with patch.object(reranker, "_load_model", return_value=None):
    clean, _ = chat_snapshot("list the deans")
    typo, _ = chat_snapshot("lost the dens")
    check("a first-turn text chat takes the identical repaired answer path", typo == clean)
    with patch.dict(namespace, {"summarizer": SimpleNamespace(invoke=unavailable_optimizer)}):
        clean, _ = chat_snapshot("list the deans")
        typo, _ = chat_snapshot("lost the dens")
        check("optimizer failure still uses the repaired question and evidence",
              typo == clean and typo["messages"][-1] == "list the deans"
              and typo["citations"] == ["current"])
    clean, _ = chat_snapshot("latin honor requirements")
    typo, _ = chat_snapshot("latin honir requirements")
    check("a repaired self-contained question uses the same optimizer skip path",
          typo == clean and typo["optimizer"] == [])
    clean, _ = chat_snapshot("list the deans", image="test-image")
    typo, state = chat_snapshot("lost the dens", image="test-image")
    check("an image question also sends the repaired wording without changing the image",
          typo == clean and state["image_data"] == "test-image")
    for question in (*KEEP_LOST, *KEEP_WANT, "Pila an bayad sa enrollment?", "what is b s i t"):
        unchanged, _ = chat_snapshot(question)
        check(f"chat keeps non-typos unchanged: {question!r}",
              unchanged["messages"][-1] == question)


print(f"\n{'='*60}")
print(f"  {_passed} passed, {_failed} failed")
print("=" * 60)

raise SystemExit(1 if _failed else 0)
