"""
rag/progress.py — telling the student what the assistant is doing, truthfully.

The problem
-----------
Between pressing Enter and the first token, `sc_assistant/chat.py` shows three
blinking dots. That is honest but uninformative: retrieval and reranking can run
for several seconds, and three dots look identical whether the pipeline is
searching Pinecone, waiting on a rewrite, or already generating. A student cannot
tell a working assistant from a hung one, so the slow answers feel broken rather
than slow.

Why this is a module and not a timer in the browser
--------------------------------------------------
The obvious cheap version is a `setInterval` in chat.js that rotates through
"Understanding…", "Searching…", "Writing…" on a script. It would be a lie. A
cached-ish answer would flash all four captions in 300 ms, and a genuinely slow
retrieval would sit on "Writing the answer" for four seconds while nothing was
being written. Worse, `rag/latency.py` now prints a real `[timing]` line for every
answer, so the invented captions would openly contradict the log.

So the captions are published by the code that actually does the work. Each one
appears when its phase truly begins, and `documents` is the real number of pages
retrieval settled on — not a guess made in JavaScript.

Design constraints this file is shaped by
-----------------------------------------
1. **It must be impossible to break an answer.** Every publish is wrapped and
   swallowed. A progress channel that raised — a full queue, a disconnected
   browser, a caption for an unknown phase — would take down a reply the student
   was otherwise about to receive. Cosmetic code does not get to do that.

2. **It must be optional.** `rag/chain.py` is also driven by
   `tools/probe_retrieval.py` and `tools/eval_retrieval.py`, which have no
   browser and no queue. `emit()` with no channel is a no-op, so those tools keep
   working unchanged and the ranking code they exercise stays identical to the
   app's.

3. **It must be testable with no network.** Same reasoning as rag/latency.py: the
   phase vocabulary and the channel are pure Python, so tests/test_progress.py
   needs no Pinecone index, no model weights and no credentials.

Threading
---------
`ProgressChannel` is written by the graph (in `app_graph.invoke()`, on the
request thread) and drained by the SSE generator. Once the pipeline moves onto a
worker thread — as the executor in chain.py already does — publishes arrive from
more than one thread, so the queue is `queue.Queue`, which is synchronised. It is
bounded: an unbounded queue behind a browser that has gone away is a slow leak,
and captions are worthless the moment they are late, so overflow is dropped
rather than buffered.
"""

from __future__ import annotations

import queue
from typing import Dict, List, Optional

# The phases, in the order a request passes through them.
#
# The wording is deliberately about the STUDENT's question and the school's
# documents, not about the machinery. "Embedding query, invoking reranker" is
# accurate and useless; a student wants to know their question is understood and
# that real pages are being read. Each caption is also short enough not to wrap on
# a phone, which is where most of this traffic is.
PHASES: Dict[str, str] = {
    # The optimizer round-trip. Only shown when it actually runs — for a
    # self-contained question rag/latency.py skips it, and claiming to "understand
    # the question" during a phase that was skipped is the exact dishonesty this
    # module exists to avoid.
    "understanding": "Understanding your question",

    # Hybrid dense + sparse retrieval against Pinecone, plus query expansion and
    # any compound-question splitting.
    "searching": "Searching Samar College documents",

    # The cross-encoder. Usually the most expensive phase, and the one that most
    # needs a caption: it is pure CPU, so there is no network activity for a
    # browser devtools panel to show either.
    "reading": "Reading the most relevant pages",

    # Prompt assembly: verified facts, freshness, calendar, announcements, role.
    "preparing": "Checking verified school information",

    # The answer LLM has been called and the first token has not arrived yet.
    "writing": "Writing the answer",
}

# Sent when a caption is not recognised. Reaching this means a phase key was
# misspelled at a call site — a mistake that must degrade to a vague-but-true
# caption, never to a KeyError inside the answer path.
FALLBACK_LABEL = "Working on it"


def label_for(phase: str, documents: int = 0, sources: int = 0) -> str:
    """
    The caption for a phase, with the real numbers folded in where they help.

    Only "reading" is given counts, and only when they are known. "Reading 8
    pages from 2 documents" tells a student something specific and verifiable —
    it is the same figure that ends up in the "Based on" footer — while
    "Searching 0 pages" during retrieval would be noise, because at that point
    nothing has been selected yet.

    Singular/plural is handled rather than printing "1 pages", which looks like
    the bug that it is.
    """
    base = PHASES.get(phase, FALLBACK_LABEL)

    if phase == "reading" and documents > 0:
        pages = f"{documents} page" + ("" if documents == 1 else "s")
        if sources > 0:
            docs = f"{sources} document" + ("" if sources == 1 else "s")
            return f"Reading {pages} from {docs}"
        return f"Reading {pages}"

    return base


class ProgressChannel:
    """
    A one-way pipe from the pipeline to one waiting browser.

    Bounded and lossy on purpose. If the consumer has gone (the student closed
    the tab mid-answer) the producer must not block and must not grow: a caption
    that cannot be delivered promptly has no value, so it is dropped. The
    alternative — an unbounded queue per abandoned request — is a leak that only
    shows up under the load of a real enrollment period.
    """

    # Five phases plus a terminator; ten leaves room for a repeat without ever
    # being large enough to matter.
    MAXSIZE = 10

    def __init__(self, maxsize: int = MAXSIZE):
        self._q: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=maxsize)
        # The last phase published, so a caller can avoid re-announcing a phase
        # it is already in (retrieval is entered once but reported from two
        # places) without every call site having to track that itself.
        self._last: str = ""

    # -- producer side (the graph) ----------------------------------------

    def publish(self, phase: str, documents: int = 0, sources: int = 0) -> None:
        """
        Announce a phase. Never raises, never blocks.

        Repeats of the phase already showing are dropped: they would make the
        caption flicker between two identical strings and waste a queue slot that
        a real transition might need.
        """
        try:
            if phase == self._last:
                return
            item = {
                "type": "phase",
                "phase": phase,
                "label": label_for(phase, documents=documents, sources=sources),
            }
            self._q.put_nowait(item)
            self._last = phase
        except queue.Full:
            # The consumer is not keeping up or has gone away. Captions are
            # worthless late, so this is dropped rather than waited on.
            pass
        except Exception:
            # Belt and braces. Nothing about a progress caption justifies
            # propagating an exception into the answer path.
            pass

    def close(self) -> None:
        """Signal that no further phases will be published."""
        try:
            self._q.put_nowait(None)
        except Exception:
            pass

    # -- consumer side (the SSE generator) --------------------------------

    def drain(self) -> List[dict]:
        """
        Every caption published since the last call, oldest first.

        Non-blocking: the SSE generator interleaves this with reading model
        tokens, so it must never wait on a phase that may not be coming. A
        `None` terminator ends the drain and is not returned.
        """
        out: List[dict] = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
            if item is None:
                break
            out.append(item)
        return out


def emit(channel: Optional[ProgressChannel], phase: str,
         documents: int = 0, sources: int = 0) -> None:
    """
    Publish a phase if there is anywhere to publish it to.

    The null-channel case is the important one: `rag/chain.py` is also run by
    tools/probe_retrieval.py and tools/eval_retrieval.py, which have no browser
    attached. Making the no-channel path a silent no-op keeps those tools on
    exactly the same code path the app uses, which is what makes them a valid
    check on retrieval behaviour.
    """
    if channel is None:
        return
    try:
        channel.publish(phase, documents=documents, sources=sources)
    except Exception:
        pass
