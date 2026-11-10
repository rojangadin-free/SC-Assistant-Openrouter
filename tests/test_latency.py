"""
test_latency.py — the answer must start sooner WITHOUT changing what it finds.

    python tests/test_latency.py

No AWS, no Pinecone, no network, no model weights. `rag/latency.py` is pure
string and clock work for exactly that reason: it decides when work is allowed to
happen, so it must be verifiable without doing any of the work.

`rag.chain` is NOT imported here. Importing it constructs the Pinecone index and
loads the embedding model at module scope, so a unit test of a skip rule would
need credentials and ~400 MB of weights. Section 5 checks the seam into chain.py
by reading the source instead, which is the same trade tests/test_pwa.py makes.

What is actually being defended
-------------------------------
1. Every textual question gets contextual interpretation, including apparently
   complete questions and real-word typos that a dictionary cannot detect.
2. Follow-ups retain access to their preceding conversation.
3. A late rewrite must be dropped, not waited for, and never crash the answer.
4. The literal question must survive. If a bug here demoted the student's own
   wording, retrieval would silently get worse in a way no timing log shows.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import concurrent.futures
import re

from rag.latency import (
    should_optimize, pick_search_query, Deadline, Timings,
)

_passed = 0
_failed = 0


def check(label, condition):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  [PASS] {label}")
    else:
        _failed += 1
        print(f"  [FAIL] {label}")


# A history that is merely non-empty. The CONTENT never matters to
# should_optimize() — only whether there was a previous turn — so a realistic
# transcript here would imply a dependency that does not exist.
HISTORY = [
    {"role": "user", "content": "what are the requirements for enrollment?"},
    {"role": "assistant", "content": "You need your Form 138, ..."},
]


print("\n=== 1. Complete questions still get contextual interpretation ===")

for q in (
    "who is the dean of the college of education",
    "what are the programs offered at samar college",
    "how much is the tuition fee for BSIT",
    "who is the principal of the elementary department",
):
    check(f"first turn, self-contained -> interpret: {q[:44]!r}",
          should_optimize(q, []) is True)

# Same question, ninth turn. "who is the dean of education" means the same thing
# on turn 1 and turn 9, so mid-conversation is not by itself a reason to wait.
check("mid-conversation and self-contained -> still interpreted",
      should_optimize("who is the dean of the college of education", HISTORY) is True)

# `None` is what a brand-new session actually passes, not [].
check("history=None behaves like no history",
      should_optimize("who is the dean of the college of education", None) is True)


print("\n=== 2. A follow-up still waits for the rewrite ================")
# The safety half. Searching these literally retrieves the previous topic's
# documents or nothing at all, which is a WRONG answer — strictly worse than the
# slow answer this change is removing. Ambiguity must resolve toward waiting.

for q in (
    "what about for transferees?",
    "how about the fee for that one",
    "is that the same for shs?",
    "and for grade 11?",
):
    check(f"follow-up -> waits: {q[:44]!r}",
          should_optimize(q, HISTORY) is True)

# A pronoun with no antecedent in the sentence is a follow-up even when the
# sentence is otherwise long enough to look self-contained.
check("anaphora mid-conversation -> waits",
      should_optimize("are they open during the semestral break", HISTORY) is True)

# Thin questions need the help whether or not there is history: two content words
# are not enough for BM25 or the cross-encoder (that is why expand_queries()
# exists), so the rewrite is worth its round-trip.
check("thin question, first turn -> waits",
      should_optimize("tuition fee?", []) is True)
check("thin question, mid-conversation -> waits",
      should_optimize("enrollment requirements", HISTORY) is True)

# Empty input asks nothing; spending an LLM call on it is indefensible.
check("empty question -> no optimizer call", should_optimize("", HISTORY) is False)
check("whitespace question -> no optimizer call", should_optimize("   ", []) is False)


print("\n=== 2b. Unresolved words are not valid search anchors =========")
for q in ("latin hnit requirements", "latin hnir requirements",
          "latin hzzzz graduation requirements"):
    check(f"unresolved term, first turn -> waits: {q!r}",
          should_optimize(q, []) is True)
    check(f"unresolved term, with history -> waits: {q!r}",
          should_optimize(q, HISTORY) is True)
check("a repaired honors question gets the same contextual validation",
      should_optimize("latin honor requirements", []) is True)
check("a known academic acronym does not bypass interpretation",
      should_optimize("BSIT admission requirements", []) is True)


print("\n=== 3. Known words and unfamiliar languages use the same path ===")
for text in ("sign the internship contact", "who is the internship contact",
             "tuition fee", "dean college education", "Pila an bayad?", "\u8bfe\u7a0b\u8981\u6c42"):
    check(f"no dictionary/thinness shortcut: {text!r}", should_optimize(text, []) is True)
check("punctuation alone has no text to interpret", should_optimize("?!", []) is False)



print("\n=== 4. The student's own wording is never lost ================")
# If a bug here demoted the literal question, retrieval would get quietly worse
# and no timing log would show it. pick_search_query() must fall back to the
# literal text for every degenerate rewrite.

check("no rewrite -> literal question",
      pick_search_query("who is the dean", None) == "who is the dean")
check("empty rewrite -> literal question",
      pick_search_query("who is the dean", "") == "who is the dean")
check("whitespace rewrite -> literal question",
      pick_search_query("who is the dean", "   ") == "who is the dean")
check("echoed rewrite -> literal question (no duplicate probe)",
      pick_search_query("who is the dean", "who is the dean") == "who is the dean")
check("echoed with different case -> still treated as an echo",
      pick_search_query("who is the dean", "WHO IS THE DEAN") == "who is the dean")
check("a real rewrite is used",
      pick_search_query("who is the dean",
                        "Samar College dean of education name")
      == "Samar College dean of education name")


print("\n=== 5. A late rewrite is dropped, not waited for ==============")
# The Deadline is what stops the optimizer from being back on the critical path
# in disguise: a rewrite that is slower than its budget must be abandoned.

# A fake clock, so the test asserts on the arithmetic rather than on sleeping.
now = [1000.0]
budget = Deadline(seconds=2.5, clock=lambda: now[0])
check("fresh deadline has its full budget", abs(budget.remaining() - 2.5) < 1e-9)
check("fresh deadline is not expired", budget.expired() is False)

now[0] += 1.0
check("partly spent deadline reports what is left",
      abs(budget.remaining() - 1.5) < 1e-9)

now[0] += 5.0
check("overspent deadline is expired", budget.expired() is True)
# Must clamp: `Future.result(timeout=-3.5)` is not a wait, and a negative value
# reaching it would be an exception instead of a graceful give-up.
check("overspent deadline never reports negative time",
      budget.remaining() == 0.0)

# A monotonic clock is required, not wall time: an NTP step mid-request must not
# expire (or un-expire) a deadline.
import time as _time
check("Deadline defaults to a monotonic clock",
      Deadline()._clock is _time.monotonic)

# End to end against a real thread: a slow rewrite times out and the caller is
# handed the literal question instead of blocking on it.
def _slow_rewrite():
    _time.sleep(0.30)
    return "a rewrite nobody will read"

with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    fut = pool.submit(_slow_rewrite)
    tiny = Deadline(seconds=0.05)
    began = _time.monotonic()
    try:
        got = fut.result(timeout=tiny.remaining())
        timed_out = False
    except concurrent.futures.TimeoutError:
        got = ""
        timed_out = True
    waited = _time.monotonic() - began

check("a rewrite slower than its budget times out", timed_out is True)
check("and the wait is bounded by the budget, not the rewrite", waited < 0.25)
check("the caller falls back to the literal question",
      pick_search_query("who is the dean", got) == "who is the dean")


print("\n=== 6. Timing instrumentation cannot break an answer =========")
# This exists to be read in a log, so a bug in it must be inert. Every method has
# to tolerate being called wrongly.

t = Timings(clock=lambda: 0.0)
t.stop("never-started")           # must not raise
check("stopping an unstarted phase is ignored", t.marks == {})

clock = [0.0]
t = Timings(clock=lambda: clock[0])
t.start("retrieval")
clock[0] = 2.0
t.stop("retrieval")
check("a phase records its duration", abs(t.marks["retrieval"] - 2.0) < 1e-9)

# A retried phase has cost the student both attempts; reporting only the second
# would hide exactly the case worth investigating.
t.start("retrieval")
clock[0] = 3.5
t.stop("retrieval")
check("a repeated phase accumulates", abs(t.marks["retrieval"] - 3.5) < 1e-9)

t.note("optimizer", "skipped")
line = t.render()
check("the log line names the phases", "retrieval=3.50s" in line)
check("the log line reports a skip", "optimizer=skipped" in line)
check("the log line reports a total", re.search(r"total=\d+\.\d\ds", line) is not None)


print("\n=== 7. The wiring in rag/chain.py is real ====================")
# The unit tests above prove the DECISIONS are right. They cannot prove chain.py
# actually honours them, and importing chain.py here would need Pinecone
# credentials and the embedding model. So the seam is checked by reading the
# source — the same approach tests/test_pwa.py uses for its templates.
#
# Every check below is a mistake that would silently restore the original wait
# while leaving all six sections above green.

with open("rag/chain.py", encoding="utf-8") as fh:
    chain_src = fh.read()

check("chain.py consults should_optimize()",
      "should_optimize(user_text, history)" in chain_src)

# The critical one. `with ThreadPoolExecutor(...)` joins every worker on exit,
# which would put the optimizer's round-trip back in front of retrieval and undo
# the whole change — with no test failure anywhere else to show it.
check("the executor is NOT used as a context manager",
      "with concurrent.futures.ThreadPoolExecutor(max_workers=3)" not in chain_src)
check("bounded pools are reused rather than joined per request",
      "executor = preparation_pool" in chain_src
      and "optimizer_pool.submit(task_query_optimization)" in chain_src
      and "executor.shutdown(" not in chain_src)

# `future_query.result()` outside the late callback is the other way to
# accidentally serialise it: the rewrite must only ever be collected with a
# timeout, from inside retrieval.
check("the rewrite is only ever collected with a timeout",
      "future_query.result(" in chain_src
      and "future_query.result()" not in chain_src)

check("interpretation is validated before it changes the working question",
      "parse_interpretation(response.content, original_text, history)" in chain_src
      and "user_text = interpretation.question" in chain_src)
check("retrieval and generation share the interpreted question",
      "user_question=user_text" in chain_src and "late_query=late_rewrite" not in chain_src)
check("a missed budget is handled, not raised",
      "concurrent.futures.TimeoutError" in chain_src)
check("the timing line is emitted", "timings.render()" in chain_src)

# retrieve_documents() must keep working for callers that pass no callback —
# tools/eval_retrieval.py and tools/probe_retrieval.py are how any ranking change
# gets caught, and they call it with the old signature.
check("late_query is optional (the eval tools still work)",
      "late_query=None" in chain_src)

# The reranker's model load must happen at boot, not inside the first student's
# question. warmup() existed for this and was called from nowhere.
with open("sc_assistant/__init__.py", encoding="utf-8") as fh:
    app_src = fh.read()
check("the reranker is warmed at startup", "warmup()" in app_src)
check("and warming cannot block the server booting",
      "daemon=True" in app_src)


print("\n" + "=" * 62)
print(f"  {_passed} passed, {_failed} failed")
print("=" * 62)

raise SystemExit(1 if _failed else 0)
