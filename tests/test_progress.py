"""
test_progress.py — the waiting caption must be TRUE, and must never cost an answer.

    python tests/test_progress.py

No AWS, no Pinecone, no network, no model weights. `rag/progress.py` is pure
queue-and-string work for exactly that reason: it describes work rather than doing
any, so it has to be verifiable without doing any either.

`rag.chain` is NOT imported. Importing it builds the Pinecone index and loads the
embedding model at module scope, so a unit test of a caption would need
credentials and ~400 MB of weights. Sections 5 and 6 check the seams into
chain.py and chat.py by reading their source, which is the same trade
tests/test_latency.py and tests/test_pwa.py already make.

What is actually being defended
-------------------------------
1. The captions say something a student can act on, and the page count is the
   real one. A caption is only worth showing if it is true; "Reading 1 pages" or
   a caption invented on a timer is worse than three honest dots.
2. The channel can NEVER break an answer. It is cosmetic code sitting directly in
   the reply path: a full queue, a closed tab or a misspelled phase key must
   degrade to a missing caption, never to a 500 on a question the handbook
   answers.
3. It stays optional. tools/probe_retrieval.py and tools/eval_retrieval.py drive
   the same graph with no browser; if a null channel ever raised, the tools we
   use to check retrieval would be the first casualty.
4. §6 is the regression that started this: the pipeline must run INSIDE the SSE
   generator. When `invoke()` ran before `Response(...)`, every phase elapsed
   before the browser had a connection to be told anything on — the captions were
   published to nobody and the UI was three static dots by construction.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import queue
import re
import threading

from rag.progress import (
    PHASES, FALLBACK_LABEL, ProgressChannel, emit, label_for,
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
    print(f"\n{'=' * 68}\n  {title}\n{'=' * 68}")


# ══════════════════════════════════════════════════════════════════════
section("1. The phase vocabulary")
# ══════════════════════════════════════════════════════════════════════
# Every phase the pipeline can report has to have a caption, and every caption
# has to be readable by a student on a phone. A missing key would fall back to
# "Working on it", which is true but says nothing — and an over-long one wraps
# the pill onto a second line over the newest message.

REQUIRED_PHASES = ["understanding", "searching", "reading", "preparing", "writing"]

for phase in REQUIRED_PHASES:
    check(f"'{phase}' has a caption", phase in PHASES)

check(
    "no caption is long enough to wrap on a phone (<= 40 chars)",
    all(len(v) <= 40 for v in PHASES.values()),
)

# The captions describe the student's question and the school's documents, not
# the machinery. "Invoking cross-encoder" is accurate and useless.
JARGON = ["embed", "rerank", "cross-encoder", "pinecone", "vector", "llm", "token"]
check(
    "no caption leaks implementation jargon",
    not any(j in v.lower() for v in PHASES.values() for j in JARGON),
)

check(
    "unknown phases degrade to a vague-but-true caption, not a KeyError",
    label_for("not-a-real-phase") == FALLBACK_LABEL,
)


# ══════════════════════════════════════════════════════════════════════
section("2. The numbers in the caption are real")
# ══════════════════════════════════════════════════════════════════════
# This is the whole reason the captions are published from the pipeline instead
# of rotated on a setInterval in the browser: the count is the pool retrieval
# actually settled on, so it matches the "Based on" footer and the [timing] log.

check(
    "the retrieved page count reaches the caption",
    "34" in label_for("reading", documents=34),
)
check(
    "the document count is included when known",
    label_for("reading", documents=8, sources=2) == "Reading 8 pages from 2 documents",
)
check(
    "one page is 'page', not 'pages'",
    label_for("reading", documents=1) == "Reading 1 page",
)
check(
    "one document is 'document', not 'documents'",
    label_for("reading", documents=3, sources=1) == "Reading 3 pages from 1 document",
)
# Before reranking there is no pool yet. "Reading 0 pages" would be a caption
# claiming a fact that does not exist, so the generic wording is used instead.
check(
    "zero pages falls back to the generic caption",
    label_for("reading", documents=0) == PHASES["reading"],
)
# Only "reading" has a count to state. Counts on the other phases would be a
# number attached to a phase that did not produce it.
check(
    "counts are not spliced into unrelated phases",
    label_for("searching", documents=34) == PHASES["searching"]
    and label_for("writing", documents=34) == PHASES["writing"],
)


# ══════════════════════════════════════════════════════════════════════
section("3. The channel: order, de-duplication, and drain semantics")
# ══════════════════════════════════════════════════════════════════════

ch = ProgressChannel()
ch.publish("searching")
ch.publish("reading", documents=12)
events = ch.drain()

check("published phases come back", len(events) == 2)
check(
    "in the order the pipeline entered them",
    [e["phase"] for e in events] == ["searching", "reading"],
)
check(
    "each event carries the phase AND its caption",
    all(e["type"] == "phase" and e["phase"] and e["label"] for e in events),
)
check("the real count survives the queue", "12" in events[1]["label"])

# Retrieval is entered once but reported from two places in chain.py. Re-sending
# the phase already showing would flicker the caption between two identical
# strings and burn a queue slot a real transition might need.
ch2 = ProgressChannel()
ch2.publish("searching")
ch2.publish("searching")
check("a repeat of the current phase is dropped", len(ch2.drain()) == 1)

# ...but a genuine return to an earlier phase is not swallowed forever, or a
# retry would go silent.
ch3 = ProgressChannel()
ch3.publish("searching")
ch3.publish("reading", documents=5)
ch3.publish("searching")
check("a real transition back is still reported", len(ch3.drain()) == 3)

# drain() must never wait: the SSE generator calls it between reading model
# tokens, so a blocking read would stall the answer to wait for a caption that
# may never come.
ch4 = ProgressChannel()
check("draining an empty channel returns nothing, immediately", ch4.drain() == [])

ch5 = ProgressChannel()
ch5.publish("writing")
ch5.drain()
check("drained events are not replayed on the next drain", ch5.drain() == [])

ch6 = ProgressChannel()
ch6.publish("searching")
ch6.close()
ch6.publish("reading", documents=3)
after_close = ch6.drain()
check(
    "close() terminates the drain instead of raising",
    [e["phase"] for e in after_close] == ["searching"],
)


# ══════════════════════════════════════════════════════════════════════
section("4. A caption can never break an answer")
# ══════════════════════════════════════════════════════════════════════
# The single most important section in this file. This code runs inside the reply
# path; if any of it can raise, a cosmetic feature is able to destroy a reply the
# student was about to receive.

# The student closed the tab mid-answer. Nobody drains, the queue fills, and the
# pipeline must carry on regardless — the answer is still being saved to their
# history.
abandoned = ProgressChannel(maxsize=2)
try:
    for i in range(50):
        abandoned.publish(f"phase-{i}")
    overflowed_cleanly = True
except Exception:
    overflowed_cleanly = False
check("a full queue is dropped, not raised", overflowed_cleanly)
check(
    "and the channel stays bounded (no slow leak per abandoned request)",
    abandoned._q.qsize() <= 2,
)

# A misspelled phase key at a call site is a typo, not an outage.
try:
    ProgressChannel().publish("compleetly-wrong")
    typo_survived = True
except Exception:
    typo_survived = False
check("an unknown phase key does not raise", typo_survived)

# Even a channel whose queue is actively hostile must not propagate.
class ExplodingQueue:
    def put_nowait(self, item):
        raise RuntimeError("boom")

    def get_nowait(self):
        raise RuntimeError("boom")

    def qsize(self):
        return 0


hostile = ProgressChannel()
hostile._q = ExplodingQueue()
try:
    hostile.publish("searching")
    hostile.close()
    drained = hostile.drain()
    hostile_survived = True
except Exception:
    hostile_survived = False
    drained = None
check("a raising queue is swallowed on publish/close", hostile_survived)
check("...and on drain, which returns nothing rather than exploding", drained == [])

# The probe/eval tools pass no channel at all. This must be a no-op, or the tools
# we use to verify retrieval would be the first thing this feature broke.
try:
    emit(None, "searching")
    emit(None, "reading", documents=9)
    null_ok = True
except Exception:
    null_ok = False
check("emit() with no channel is a silent no-op (probe/eval tools)", null_ok)

live = ProgressChannel()
emit(live, "reading", documents=7, sources=2)
emitted = live.drain()
check(
    "emit() forwards phase and counts when a channel IS attached",
    len(emitted) == 1 and "7 pages from 2 documents" in emitted[0]["label"],
)

# chain.py already hands retrieval to a thread pool, so publishes arrive from
# more than one thread. A plain list here would silently lose events.
concurrent_ch = ProgressChannel(maxsize=200)


def _spam(n):
    for i in range(20):
        concurrent_ch.publish(f"p{n}-{i}")


threads = [threading.Thread(target=_spam, args=(n,)) for n in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(
    "concurrent publishes from worker threads are not lost or corrupted",
    len(concurrent_ch.drain()) == 100,
)
check("the queue is a synchronised queue.Queue", isinstance(ProgressChannel()._q, queue.Queue))


# ══════════════════════════════════════════════════════════════════════
section("5. The seam into the pipeline (source-read)")
# ══════════════════════════════════════════════════════════════════════
# Read as text on purpose — see this file's docstring. What matters is that the
# captions are published from the code that does the work, at the boundary where
# each phase truly begins.

chain_src = open("rag/chain.py", encoding="utf-8").read()

check(
    "chain.py publishes phases",
    "from rag.progress import emit as emit_phase" in chain_src,
)

# The channel must NOT be a state field. The graph is compiled with a
# checkpointer, so the whole state is serialised after every step — and a live
# `queue.Queue` bound to one HTTP connection is not msgpack-serialisable. When it
# lived in state, EVERY answer died with `Type is not msgpack serializable:
# ProgressChannel` and the student saw "Sorry, something went wrong while
# preparing that answer." A cosmetic caption took down the whole reply path,
# which is the one thing rag/progress.py is written to be incapable of.
state_block = chain_src[chain_src.find("class ChatState"):chain_src.find("def docs_to_context")]
check(
    "the channel is NOT a checkpointed state field",
    "progress: Optional" not in state_block,
)
check(
    "...and the node reads it from config instead",
    'config or {}).get("configurable", {}).get("progress")' in chain_src,
)
check(
    "...which means the node accepts config",
    "def call_llm(state: ChatState, config=None)" in chain_src,
)

for phase in ["searching", "reading", "preparing", "understanding"]:
    check(
        f"chain.py reports '{phase}' from the work itself",
        re.search(rf'emit_phase\(\s*progress,\s*"{phase}"', chain_src) is not None
        or re.search(rf'emit_phase\(progress,\s*"{phase}"', chain_src) is not None,
    )

# The count must come from the retrieved pool, not from a literal.
check(
    "the 'reading' count is the real retrieved pool size",
    "documents=len(initial_docs)" in chain_src,
)

# "understanding" must be conditional: rag/latency.py SKIPS the optimizer for a
# self-contained question, and announcing a phase that was skipped is precisely
# the dishonesty this module exists to avoid.
check(
    "'understanding' is only reported when the rewrite actually runs",
    re.search(
        r"if wants_rewrite:\s*\n\s*emit_phase\(progress,\s*\"understanding\"\)",
        chain_src,
    )
    is not None,
)


# ══════════════════════════════════════════════════════════════════════
section("6. The seam into the SSE route (source-read)")
# ══════════════════════════════════════════════════════════════════════
# The original bug, guarded so it cannot come back. `app_graph.invoke()` used to
# be called before `Response(...)` was returned, which meant retrieval and
# reranking — the seconds a student actually waits — all elapsed while the
# browser was still waiting for response headers. There was no open connection to
# publish to, so no caption could ever be delivered.

chat_src = open("sc_assistant/chat.py", encoding="utf-8").read()

check(
    "the route creates a channel and passes it to the graph",
    "ProgressChannel()" in chat_src and '"progress": progress,' in chat_src,
)
# The other half of the msgpack crash. The channel must ride in `configurable`,
# which is per-invocation wiring, and must never be put in the payload, which is
# state and therefore checkpointed.
check(
    "...via config['configurable'], not the checkpointed payload",
    '"configurable"' in chat_src and 'input_payload["progress"]' not in chat_src,
)


gen_start = chat_src.find("def generate():")
check("the SSE generator exists", gen_start != -1)
invoke_at = chat_src.find("app_graph.invoke(input_payload", gen_start)
check(
    "the pipeline runs INSIDE the generator, so phases have somewhere to go",
    invoke_at != -1,
)
check(
    "the generator forwards every published caption to the browser",
    "progress.drain()" in chat_src[gen_start:],
)
# The first caption must be flushed BEFORE the pipeline starts, or the browser
# paints nothing until the first phase completes — which on a slow retrieval is
# the entire wait.
first_yield = chat_src.find("'phase'", gen_start)
check(
    "a caption is flushed before the pipeline blocks",
    first_yield != -1 and first_yield < invoke_at,
)
# 'writing' is the one phase the graph cannot publish itself: the answer model is
# invoked out in the route, after the graph has returned.
check(
    "the route itself announces 'writing' before the first token",
    "'phase': 'writing'" in chat_src,
)


# ══════════════════════════════════════════════════════════════════════
section("7. The seam into the UI (source-read)")
# ══════════════════════════════════════════════════════════════════════

html_src = open("sc_assistant/templates/chat.html", encoding="utf-8").read()
js_src = open("sc_assistant/static/js/chat.js", encoding="utf-8").read()
css_src = open("sc_assistant/static/css/chat.css", encoding="utf-8").read()

# These four used to read chat.html. The indicator is no longer a fixed element
# in the page: it is built per request INSIDE the assistant's bubble, so the
# markup lives in chat.js and the template is the wrong file to assert against.
check("the typing bubble has a caption slot", 'id="typingPhase"' in js_src)
check(
    "the caption is announced to screen readers",
    re.search(r'id="typingPhase"[^>]*aria-live', js_src) is not None
    or re.search(r'aria-live[^>]*id="typingPhase"', js_src) is not None,
)
# The dots had to be wrapped: the blink rules used to target every child <span>,
# so the caption itself would have been styled as a fourth 6px dot.
check("the dots are wrapped so the caption is not styled as a dot", "typing-dots" in js_src)
check("...and the CSS blinks only the wrapped dots", ".typing-dots span" in css_src)

# Where the dots appear is a promise to the student: they mark the place the
# answer is about to be written. The old pill floated above the input, ~100px
# from that place. This asserts the indicator is emitted into the bubble markup
# rather than anywhere else in the file.
bubble_at = js_src.find("class=\"message assistant is-thinking\"")
check("the dots are built inside the assistant's bubble", bubble_at != -1)
check(
    "...within the message-bubble, not beside it",
    bubble_at != -1
    and re.search(
        r'class="message-bubble">\s*\n\s*\$\{thinkingBubble\(\)\}',
        js_src[bubble_at:bubble_at + 700],
    )
    is not None,
)
# The template must not keep a second copy. Two indicators sharing one id is a
# caption written to whichever the browser found first.
check(
    "the template no longer carries its own indicator",
    'id="typingPhase"' not in html_src and 'id="typingIndicator"' not in html_src,
)

check("chat.js handles the phase event", "data.type === 'phase'" in js_src)
# A stale caption is now impossible by construction rather than by remembering to
# blank it: the indicator is created with the bubble and removed from the DOM with
# the first token, so there is no shared node to inherit the previous page count.
check(
    "the caption cannot survive into the next question",
    "$ind.remove()" in js_src and "clearTypingIndicator()" in js_src,
)
# Removed, not hidden. Hidden would leave a blank flex row above the answer for
# the life of the message, because the indicator is inside the bubble now.
check(
    "the indicator is removed rather than hidden",
    "typingIndicator.hide()" not in js_src,
)

check(
    "phases are ignored once the answer has begun streaming",
    re.search(r"if \(data\.type === 'phase'\) \{\s*\n\s*if \(isFirstToken\)", js_src)
    is not None,
)


# ══════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 68}")
print(f"  {_passed} passed, {_failed} failed")
print(f"{'=' * 68}\n")
raise SystemExit(1 if _failed else 0)
