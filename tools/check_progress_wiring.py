"""
check_progress_wiring.py — prove a ProgressChannel can survive the checkpointer.

    python tools/check_progress_wiring.py

Why this is a separate tool and not part of tests/test_progress.py
-----------------------------------------------------------------
The unit suite must run with no credentials and no model weights, so it reads
rag/chain.py as TEXT. That is enough to catch the channel reappearing as a state
field, but it can never catch the actual failure mode, because the failure was
not in our code at all — it was LangGraph msgpack-ing the state into a
checkpoint after the node returned:

    Type is not msgpack serializable: ProgressChannel

A source-read cannot observe that. Only running a graph WITH a checkpointer and a
live channel can. So this builds a miniature graph with the same shape as the
real one — `StateGraph` + `InMemorySaver`, channel passed through
`config["configurable"]` — and invokes it. No Pinecone, no embeddings, no
network: the node does nothing but publish a caption, which is precisely the part
that used to poison the checkpoint.

Exit code is 0 when the wiring holds, 1 when it does not, so this can be dropped
into CI next to the unit suite.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path, CWD at the repo root

from typing import TypedDict, Optional, List, Dict

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver

from rag.progress import ProgressChannel, emit as emit_phase


class MiniState(TypedDict):
    """
    The same shape as rag/chain.py's ChatState in the one respect that matters:
    everything in it is plain data. If a field here held the channel, the
    invoke() below would raise the msgpack error — that is the point.
    """
    input: str
    answer: str
    chat_history: List[Dict[str, str]]
    citations: Optional[List[Dict[str, object]]]


def build():
    graph = StateGraph(MiniState)

    def node(state: MiniState, config=None):
        # Read exactly the way rag/chain.py's call_llm does.
        progress = (config or {}).get("configurable", {}).get("progress")
        emit_phase(progress, "searching")
        emit_phase(progress, "reading", documents=12, sources=2)
        emit_phase(progress, "preparing", documents=12, sources=2)
        return {"answer": "ok", "citations": []}

    graph.add_node("n", node)
    graph.set_entry_point("n")
    graph.add_edge("n", END)
    # The checkpointer is the whole reason this file exists. Without it the
    # state is never serialised and the bug is invisible.
    return graph.compile(checkpointer=InMemorySaver())


def main() -> int:
    app = build()
    progress = ProgressChannel()

    config = {
        "configurable": {
            "thread_id": "wiring-check",
            # Not checkpointed — this is the fix being verified.
            "progress": progress,
        }
    }
    payload = {"input": "test", "chat_history": [], "citations": []}

    print("Invoking a checkpointed graph with a live ProgressChannel...")
    try:
        result = app.invoke(payload, config=config)
    except Exception as e:
        print(f"\nFAIL  the graph could not be invoked: {type(e).__name__}: {e}")
        if "msgpack" in str(e).lower():
            print("      This is the original bug: the channel is reaching the")
            print("      checkpointer. It must travel in config['configurable'],")
            print("      never in the state/payload.")
        return 1

    ok = True

    if result.get("answer") != "ok":
        print("FAIL  the node did not return normally")
        ok = False
    else:
        print("PASS  the node ran and the state was checkpointed without error")

    # A second turn on the SAME thread, which is what a real conversation does.
    # This is where a checkpoint is not just written but READ BACK, so a value
    # that only *serialised* by luck would fail here instead.
    try:
        app.invoke({"input": "follow-up"}, config=config)
        print("PASS  a second turn on the same thread replays the checkpoint")
    except Exception as e:
        print(f"FAIL  replaying the thread failed: {type(e).__name__}: {e}")
        ok = False

    # And the captions must actually have gone somewhere. A "fix" that routed the
    # channel out of state but never delivered it to the node would pass every
    # check above and still leave the student watching three static dots.
    events = progress.drain()
    phases = [e["phase"] for e in events]
    if phases[:3] == ["searching", "reading", "preparing"]:
        print(f"PASS  captions arrived in order: {phases}")
    else:
        print(f"FAIL  captions did not arrive as expected: {phases}")
        ok = False

    labels = [e["label"] for e in events]
    if any("12 pages from 2 documents" in l for l in labels):
        print("PASS  the real page/document counts survived the trip")
    else:
        print(f"FAIL  counts were lost: {labels}")
        ok = False

    # The inverse experiment: putting the channel in the PAYLOAD must still fail.
    # If this ever starts passing, the constraint that shaped the design has gone
    # away and this file should be revisited rather than trusted.
    print("\nControl: the same channel in the checkpointed payload...")
    try:
        build().invoke(
            {"input": "x", "chat_history": [], "citations": [], "progress": progress},
            config={"configurable": {"thread_id": "control"}},
        )
        print("NOTE  the payload route did NOT fail — LangGraph's serialisation")
        print("      behaviour may have changed. config['configurable'] is still")
        print("      the correct home for a per-request object.")
    except Exception as e:
        print(f"PASS  it fails, as expected: {type(e).__name__}: {str(e)[:80]}")

    print("\n" + ("All good — the channel is routed around the checkpointer."
                  if ok else "Wiring is broken. See failures above."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
