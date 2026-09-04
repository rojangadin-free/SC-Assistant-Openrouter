"""
The phase captions must survive the trip to a real browser.

Why this suite exists
---------------------
Every caption was correct, published at a real phase boundary, and invisible in
the deployed app. Three separate reasons, none of which reproduce on localhost:

1. `app_graph.invoke()` was called straight from the SSE generator and the
   captions were drained AFTERWARDS. A synchronous call cannot be interleaved
   with, so the entire wait — retrieval, reranking, the part with the spinner —
   produced no output at all, and then every caption arrived in one burst
   milliseconds before the first token. "Reading 34 pages from 3 documents" was
   replaced by the answer in the same frame it appeared. The pipeline now runs on
   a worker thread and the generator forwards captions while it works.

2. Nothing told intermediaries to leave the stream alone. A proxy that buffers a
   response until its own buffer fills, or that gzips it (which requires
   buffering), holds the first caption for the whole retrieval and releases it
   together with the answer. `X-Accel-Buffering: no`, `no-transform`, a 2 KB
   comment preamble and a periodic heartbeat all exist to keep bytes moving.

3. The browser parsed each `reader.read()` as if it were one SSE event: split on
   '\n', then `JSON.parse` anything starting with 'data: '. A read is a TCP read.
   Two events can arrive together and one event can be cut in half, and half a
   line threw a SyntaxError out of the read loop, killing the rest of the stream.
   Local Flask writes each yield in its own read, so this never happened in
   development and was routine through a proxy.

These are source-text assertions. What they actually guard is a set of
DECISIONS — run the pipeline off-thread, keep the stream unbuffered, parse
incrementally — each of which is easy to undo by accident while refactoring, and
none of which shows a symptom until the app is behind a proxy.
"""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:  # keeps the repo root importable however the runner invokes us
    import _bootstrap  # noqa: F401
except Exception:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

failures = []


def check(label, ok, detail=""):
    if ok:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label}{(' -> ' + detail) if detail else ''}")


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


print("\nSSE caption delivery")
print("-" * 60)

server = read("sc_assistant/chat.py")
client = read("sc_assistant/static/js/chat.js")

# -- 1. captions are streamed WHILE the pipeline runs ----------------------
check(
    "the pipeline runs on a worker thread",
    "threading.Thread(" in server and "app_graph.invoke(input_payload" in server,
)
check(
    "the generator polls for captions while the pipeline runs",
    bool(re.search(r"while not \w+\.wait\(", server)),
)
check(
    "captions are drained inside that loop, not only after the answer",
    bool(
        re.search(
            r"while not \w+\.wait\([^)]*\):\s*\n\s*for event in progress\.drain\(\)",
            server,
        )
    ),
)
check(
    "a failure on the worker thread still reaches the browser as an error event",
    'box.get("error")' in server and "'type': 'error'" in server,
)

# -- 2. nothing between Flask and the browser may buffer ------------------
check(
    "the stream asks proxies not to buffer",
    "'X-Accel-Buffering': 'no'" in server,
)
check(
    "the stream asks proxies not to transform (gzip forces buffering)",
    "no-transform" in server,
)
check(
    "a comment preamble is flushed before the first caption",
    bool(re.search(r'yield ":" \+ " " \* \d{3,}', server)),
)
check(
    "a heartbeat keeps the connection alive during the silent phases",
    "keep-alive\\n\\n" in server or ": keep-alive" in server,
)
check(
    "the response is still an event stream",
    "mimetype='text/event-stream'" in server,
)

# -- 3. the client parses a byte stream, not a sequence of events ---------
check(
    "the client carries partial lines across reads",
    "sseBuffer" in client and "lines.pop()" in client,
)
check(
    "the client never JSON.parses a line it has not fully received",
    bool(re.search(r"sseBuffer\s*=\s*lines\.pop\(\)", client)),
)
check(
    "a malformed event cannot kill the rest of the stream",
    bool(re.search(r"try\s*\{\s*\n?\s*data = JSON\.parse", client)),
)
check(
    "the caption element is still updated from phase events",
    "data.type === 'phase'" in client and "setTypingPhase(data.label)" in client,
)

print("-" * 60)
if failures:
    print(f"  {len(failures)} check(s) failed\n")
    sys.exit(1)
print("  all checks passed\n")
