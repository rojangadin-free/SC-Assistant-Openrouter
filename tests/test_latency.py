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
1. Self-contained questions skip an unnecessary model call; thin questions and
   corpus-supported misspellings retain the reference optimizer opportunity.
2. Follow-ups retain access to their preceding conversation.
3. A late rewrite must be dropped, not waited for, and never crash the answer.
4. The literal question must survive. If a bug here demoted the student's own
   wording, retrieval would silently get worse in a way no timing log shows.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import ast
import concurrent.futures
import os
import re
from pathlib import Path
from unittest.mock import patch

from rag.latency import (
    should_optimize, pick_search_query, Deadline, Timings, OPTIMIZER_BUDGET_SECONDS,
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


print("\n=== 1. Complete questions skip the optional optimizer =========")

for q in (
    "who is the dean of the college of education",
    "what are the programs offered at samar college",
    "how much is the tuition fee for BSIT",
    "who is the principal of the elementary department",
):
    check(f"first turn, self-contained -> skip: {q[:44]!r}",
          should_optimize(q, []) is False)

# Same question, ninth turn. "who is the dean of education" means the same thing
# on turn 1 and turn 9, so mid-conversation is not by itself a reason to wait.
check("mid-conversation and self-contained -> still skipped",
      should_optimize("who is the dean of the college of education", HISTORY) is False)

# `None` is what a brand-new session actually passes, not [].
check("history=None behaves like no history",
      should_optimize("who is the dean of the college of education", None) is False)


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


print("\n=== 2b. Corpus-supported misspellings still request a rewrite ==")
for q in ("latin honir requirements", "enrolmnet admission requirements"):
    check(f"unresolved term, first turn -> waits: {q!r}",
          should_optimize(q, []) is True)
    check(f"unresolved term, with history -> waits: {q!r}",
          should_optimize(q, HISTORY) is True)
check("a repaired three-term honors question skips the extra round trip",
      should_optimize("latin honor requirements", []) is False)
check("a known acronym with sufficient content skips the extra round trip",
      should_optimize("BSIT admission requirements", []) is False)
from rag.spelling import correct_query, has_unresolved_spelling_hint

typo_question = "how do i become latin honot"
check("honot remains too ambiguous for automatic local correction",
      correct_query(typo_question) == "")
check("a close unresolved honors typo requests interpretation",
      has_unresolved_spelling_hint(typo_question))
for history in ([], HISTORY):
    check(f"the exact honot question reaches the optimizer, history={bool(history)}",
          should_optimize(typo_question, history) is True)
for q in ("how do i become latin honor", "how do i become latin honors",
          "latin history admission requirements", "latin hint admission requirements",
          "latin HONOT admission requirements", "latin Honot admission requirements",
          "latin hzzzz admission requirements", "latin honot101 admission requirements"):
    check(f"known words, names, codes and weak hints do not force interpretation: {q!r}",
          has_unresolved_spelling_hint(q) is False)
check("the corrected complete question still skips interpretation",
      should_optimize("how do i become latin honors", []) is False)
for q in ("dean of bscrim pls who?", "dean of bscrim please, who?", "pls dean of bscrim plz"):
    check(f"courtesy does not make a thin dean question look complete: {q!r}",
          should_optimize(q, []) == should_optimize("dean of bscrim", []))
with patch("rag.latency.has_misspelling", return_value=True):
    check("the misspelling signal precedes first-turn thinness",
          should_optimize("four otherwise searchable content terms", []) is True)


print("\n=== 3. Reference thinness and context heuristics are retained ==")
with patch("rag.latency.has_misspelling", return_value=False):
    for text, expected in (("tuition fee", True), ("dean college education", False),
                           ("Pila an bayad?", True), ("\u8bfe\u7a0b\u8981\u6c42", True), ("?!", True)):
        check(f"reference term-count decision: {text!r}", should_optimize(text, []) is expected)
    for text in ("yes do the programs offered", "sure what are the fees", "no tell me the requirements"):
        check(f"a reply with history requests context: {text!r}", should_optimize(text, HISTORY) is True)
    check("pronouns without history do not force a complete question rewrite",
          should_optimize("are they open during the semestral break", []) is False)
    check("whole-word anaphora does not match substrings",
          should_optimize("thatch roofing scholarship requirements", HISTORY) is False)



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
tree = ast.parse(Path("rag/latency.py").read_text(encoding="utf-8"))
assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "OPTIMIZER_BUDGET_SECONDS"
                          for target in node.targets))
namespace = {"os": os}
compiled = compile(ast.Module(body=[assignment], type_ignores=[]), "rag/latency.py", "exec")
with patch.dict(os.environ, {}, clear=True):
    exec(compiled, namespace)
    check("the default optimizer wait matches the reference 2.5 seconds",
          namespace["OPTIMIZER_BUDGET_SECONDS"] == 2.5)
with patch.dict(os.environ, {"SC_OPTIMIZER_BUDGET_SECONDS": "0.75"}):
    exec(compiled, namespace)
    check("the deployment can override the optimizer wait",
          namespace["OPTIMIZER_BUDGET_SECONDS"] == 0.75)
check("Deadline uses the configured optimizer budget",
      Deadline.__init__.__defaults__[0] == OPTIMIZER_BUDGET_SECONDS)
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
completed = concurrent.futures.Future()
completed.set_result("a rewrite already available")
check("a completed rewrite can be read at zero remaining wait, as in the reference",
      completed.result(timeout=0) == "a rewrite already available")
pending = concurrent.futures.Future()
try:
    pending.result(timeout=0)
    timed_out = False
except concurrent.futures.TimeoutError:
    timed_out = True
check("a pending rewrite is not waited for after the budget expires", timed_out)


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

check("chain.py optimizes every nonempty text query",
      "if user_text.strip():" in chain_src)

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

check("the sequential optimizer supplies the corrected working question",
      "optimize_queries(query_optimizer_model, user_text, history)" in chain_src
      and "user_text = corrected_query" in chain_src)
check("retrieval retains the corrected question and contextualized companion",
      "user_question=user_text" in chain_src and "contextual_query or standalone_query," in chain_src)
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
