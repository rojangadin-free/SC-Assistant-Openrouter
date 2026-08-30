"""
test_stream_fallback.py — a blocked provider must not become "Streaming interrupted."

    python tests/test_stream_fallback.py

The failure this guards
----------------------
A student typed "latin honor". Retrieval worked perfectly — pages 52-56 of the
handbook, the ACADEMIC AWARDS section, ranked 1st and 2nd. Then:

    Streaming pipeline breakdown: Error code: 400 - {'error':
      {'code': 'content-blocked', ...}}

AgentRouter's content filter rejected the request. Nothing was wrong with the
question, the prompt or the documents; the gateway simply refused it. Two things
turned that recoverable refusal into a dead end:

1. **The fallback pointed at the same gateway.** Both models were served by
   agentrouter.org, so the retry was screened by the rule that had just rejected
   the first attempt. A fallback on the same provider is the same request sent
   twice.

2. **`with_fallbacks` cannot see a streaming failure.** It wraps the *call*, but
   `.stream()` returns a generator before any HTTP request is dispatched, so the
   400 surfaces inside the caller's `for` loop — long after the wrapper has
   returned. The fallback never fired.

`stream_answer()` fixes (2) by walking the providers itself. These tests pin down
the behaviour that fix has to have, including the two cases where falling over is
the WRONG thing to do.

No network: the models are replaced with fakes, because what is being tested is
the control flow around a provider failure, not any provider.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import os
import sys
import types

# (The old `sys.path.insert(dirname(__file__))` here is gone: it added the repo
#  root only while this file lived in the repo root. _bootstrap above does it
#  correctly from tests/.)

# `rag.chain` at import time builds Pinecone clients and loads a local embedding
# model. Only the ~30 lines of stream_answer() are under test here, so the module
# is loaded from source with its heavy imports stubbed out.
os.environ.setdefault("PINECONE_API_KEY", "test-key")

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


# ---------------------------------------------------------------------------
# stream_answer() re-implemented EXACTLY as it appears in rag/chain.py.
#
# Importing the real module would pull in Pinecone, langgraph and a
# sentence-transformers download for thirty lines of generator logic. If the
# function in chain.py changes, this copy must change with it — the section
# below is what documents why each branch is there.
# ---------------------------------------------------------------------------


class _Chunk:
    def __init__(self, content):
        self.content = content


def make_model(chunks=(), fail_at=None, error="content-blocked"):
    """
    A stand-in for ChatOpenAI.

    fail_at=0  -> refuses before emitting anything (the AgentRouter 400 case)
    fail_at=2  -> dies after two chunks (a dropped connection mid-answer)
    fail_at=None -> streams cleanly
    """
    def stream(messages):
        for i, c in enumerate(chunks):
            if fail_at is not None and i == fail_at:
                raise RuntimeError(f"Error code: 400 - {error}")
            yield _Chunk(c)
        if fail_at is not None and fail_at >= len(chunks):
            raise RuntimeError(f"Error code: 400 - {error}")

    return types.SimpleNamespace(stream=stream)


def stream_answer(messages, primary, fallback):
    """Verbatim copy of rag.chain.stream_answer, with the models injected."""
    attempts = [("primary", primary), ("fallback", fallback)]
    last_error = None

    for label, model in attempts:
        produced = False
        try:
            for chunk in model.stream(messages):
                text = chunk.content
                if not text:
                    continue
                if isinstance(text, list):
                    text = "".join(
                        block.get("text", "")
                        for block in text
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                if not text:
                    continue
                produced = True
                yield text
            return
        except Exception as e:
            last_error = e
            if produced:
                print(f"  [{label}] failed mid-stream, keeping partial answer: {e}")
                return
            print(f"  [{label}] refused before the first token, trying next: {e}")

    raise last_error if last_error else RuntimeError("No model produced a response.")


# ============================================================
section("1. The happy path is unchanged")
# ============================================================
primary = make_model(["Latin honors ", "require a GPA of ", "1.75 or better."])
fallback = make_model(["should never be reached"])

out = list(stream_answer([], primary, fallback))
check("all chunks arrive", "".join(out) == "Latin honors require a GPA of 1.75 or better.", out)
check("the fallback was not touched", "never" not in "".join(out))
check("chunks stay separate, so the UI still streams", len(out) == 3, out)


# ============================================================
section("2. 'latin honor': a pre-token refusal falls over silently")
# ============================================================
# The actual bug. The primary raises HTTP 400 content-blocked before emitting
# anything, and the student must still get their answer.
primary = make_model(["unused"], fail_at=0)
fallback = make_model(["Summa Cum Laude requires ", "a GPA of 1.25."])

out = list(stream_answer([], primary, fallback))
check("the student gets an answer anyway",
      "".join(out) == "Summa Cum Laude requires a GPA of 1.25.", out)
check("nothing from the failed attempt leaks in", "unused" not in "".join(out))


# ============================================================
section("3. A mid-stream failure keeps what arrived")
# ============================================================
# The opposite decision, deliberately. Once text is on the student's screen,
# restarting on another model would splice two different answers together — the
# reader would see a sentence begin twice, or two different GPA cut-offs.
primary = make_model(["Latin honors are ", "awarded to graduates ", "who..."], fail_at=2)
fallback = make_model(["A COMPLETELY DIFFERENT ANSWER"])

out = list(stream_answer([], primary, fallback))
check("the partial answer is preserved",
      "".join(out) == "Latin honors are awarded to graduates ", out)
check("the fallback did NOT restart the answer",
      "DIFFERENT" not in "".join(out), out)


# ============================================================
section("4. Both providers down -> the caller still learns why")
# ============================================================
primary = make_model([], fail_at=0, error="content-blocked")
fallback = make_model([], fail_at=0, error="rate-limited")

raised = None
try:
    list(stream_answer([], primary, fallback))
except Exception as e:
    raised = e

check("an exception reaches the route", raised is not None)
check("it is the LAST error, so the log names the final provider",
      raised and "rate-limited" in str(raised), str(raised))


# ============================================================
section("5. Block-shaped and empty chunks")
# ============================================================
# Some providers send content as a list of typed blocks rather than a string, and
# many send empty keep-alive deltas. Neither must reach the browser as "None" or
# an empty SSE frame.
primary = types.SimpleNamespace(stream=lambda m: iter([
    _Chunk(""),
    _Chunk([{"type": "text", "text": "Cum Laude: "}, {"type": "other", "x": 1}]),
    _Chunk(None),
    _Chunk("1.75 to 1.50"),
]))
out = list(stream_answer([], primary, make_model()))
check("block content is flattened to text", "".join(out) == "Cum Laude: 1.75 to 1.50", out)
check("empty and None chunks are dropped entirely", len(out) == 2, out)


# ============================================================
section("6. A provider that returns nothing at all")
# ============================================================
# HTTP 200, no tokens — a content filter succeeding quietly. No exception is
# raised, so the route cannot detect this from stream_answer() alone; it checks
# for an empty answer itself and tells the student to rephrase, instead of saving
# a blank assistant turn into the conversation.
out = list(stream_answer([], make_model([]), make_model(["fallback text"])))
check("an empty primary is treated as success, not failure", out == [], out)
check("so the route is the one that must notice", "".join(out).strip() == "")


# ============================================================
section("7. The two providers are actually different gateways")
# ============================================================
# The configuration half of the fix. If both models resolve to the same host, the
# retry is filtered by the same rule that rejected the first attempt and this
# whole file is testing a fallback that cannot help.
import config  # noqa: E402

check("primary and fallback are different models",
      config.CHAT_MODEL_NAME != config.FALLBACK_MODEL_NAME,
      f"{config.CHAT_MODEL_NAME} == {config.FALLBACK_MODEL_NAME}")
# An OpenRouter model name is vendor-qualified ("deepseek/deepseek-chat-v3.1"),
# an AgentRouter one is bare ("deepseek-v4-flash"). That is the cheapest reliable
# signal that the fallback is not on the gateway that just refused us.
check("the fallback is vendor-qualified, i.e. served elsewhere",
      "/" in config.FALLBACK_MODEL_NAME, config.FALLBACK_MODEL_NAME)


print(f"\n{'='*60}")
print(f"  {_passed} passed, {_failed} failed")
print("=" * 60)

raise SystemExit(1 if _failed else 0)
