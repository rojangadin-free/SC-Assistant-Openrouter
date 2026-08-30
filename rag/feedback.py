"""
rag/feedback.py — one-click 👍/👎 on every answer, and what to do with it.

Why this exists
---------------
The report modal already catches serious complaints, but it costs the student a
form: pick a reason, type an explanation, submit. Almost nobody does that for the
answer that was *technically correct and quietly useless* — which is the failure
mode you most need to see, and the one you are currently blind to.

A thumb is one click. That difference is the whole feature: it is the only signal
cheap enough that students actually give it.

What makes this more than a vanity counter
------------------------------------------
Each vote is stored **with the retrieved sources that produced the answer**. That
turns the log into three things at once:

1. a quality metric per topic (which subjects answer badly);
2. a suspect list of *documents* (a file that keeps appearing under 👎 is either
   wrong, stale, or badly chunked);
3. a regression set for `eval_retrieval.py` — every 👍 is a question whose
   retrieval is known-good and must keep working after a reranker or chunking
   change, and every 👎 is a case to fix.

(3) is the reason the sources are stored rather than just the verdict. A vote
without its evidence tells you something is wrong but never what.

Design notes
------------
* **Topic grouping is reused from `rag.gaps`.** Voting rows must group the same
  way gap rows do, or the two admin screens would disagree about what "a topic"
  is. One normalizer, one definition.

* **Same JSON+lock storage as conflicts/gaps**, so this codebase has one storage
  story. `record_vote()` never raises — it is called from the chat path, where a
  logging failure must never cost a student their answer.

* **One vote per message.** Re-voting the same `msg_id` overwrites rather than
  appends, so a student flipping 👎 to 👍 corrects the record instead of stuffing
  the ballot.
"""

from __future__ import annotations

import datetime
import os
import threading
from typing import Dict, List, Optional

from rag.gaps import normalize_question
from rag.store import JsonBlobStore


FEEDBACK_FILE = os.getenv("ANSWER_FEEDBACK_FILE", "answer_feedback.json")

_lock = threading.Lock()

# Bounded on purpose: this drives prioritisation and a regression set, not an
# audit trail. Oldest votes are dropped first.
MAX_VOTES = 2000

# Per topic, keep only enough examples for a human to see the pattern.
MAX_EXAMPLES_PER_TOPIC = 8

VALID_VERDICTS = ("up", "down")


# ============================================================
# STORAGE
# ============================================================

def _empty_store() -> dict:
    return {"votes": {}, "topics": {}, "updated_at": ""}


# Delegated to `rag.store` (same file by default, DynamoDB when
# STORE_BACKEND=dynamodb). Sharing matters here because a vote is only useful in
# aggregate: split across two containers, "3 up / 4 down" and "2 up / 1 down"
# each look like too little data to act on, when together they are a clear
# failing topic. The regression export has the same problem — half the
# known-good questions would be missing from whichever instance runs it.
_store_obj = None
_store_obj_path = None
_store_init_lock = threading.Lock()


def _store():
    global _store_obj, _store_obj_path
    path = os.getenv("ANSWER_FEEDBACK_FILE", FEEDBACK_FILE)
    with _store_init_lock:
        if _store_obj is None or _store_obj_path != path:
            _store_obj = JsonBlobStore("feedback", path, _empty_store)
            _store_obj_path = path
        return _store_obj


def _load() -> dict:
    data = _store().load()
    if "votes" not in data:
        return _empty_store()
    data.setdefault("topics", {})
    return data


def _save(store: dict) -> None:
    _store().save(store)



def _prune_votes(votes: dict) -> dict:
    if len(votes) <= MAX_VOTES:
        return votes
    ordered = sorted(votes.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)
    return dict(ordered[:MAX_VOTES])


def _rebuild_topic(topic_key: str, votes: dict) -> dict:
    """
    Recompute one topic's aggregate from the raw votes.

    Derived rather than incremented, because a student changing their vote has to
    *decrement* the old verdict. Counters that only ever go up would drift out of
    agreement with the votes they claim to summarise, and a metric you cannot
    trust is worse than no metric.
    """
    mine = [v for v in votes.values() if v.get("topic_key") == topic_key]
    up = sum(1 for v in mine if v.get("verdict") == "up")
    down = sum(1 for v in mine if v.get("verdict") == "down")
    total = up + down

    mine.sort(key=lambda v: v.get("at", ""), reverse=True)
    examples = [
        {
            "question": v.get("question", ""),
            "verdict": v.get("verdict", ""),
            "comment": v.get("comment", ""),
            "sources": v.get("sources", []),
            "answer_snippet": v.get("answer_snippet", ""),
            "at": v.get("at", ""),
        }
        for v in mine[:MAX_EXAMPLES_PER_TOPIC]
    ]

    # Which documents keep showing up under a downvote. This is the actionable
    # part: it points at a file and a page, not just at a bad feeling.
    suspect: Dict[str, int] = {}
    for v in mine:
        if v.get("verdict") != "down":
            continue
        for s in v.get("sources", []):
            label = s if isinstance(s, str) else ""
            if label:
                suspect[label] = suspect.get(label, 0) + 1

    return {
        "key": topic_key,
        "topic": mine[0].get("question", "") if mine else "",
        "up": up,
        "down": down,
        "total": total,
        "satisfaction": round(up / total, 3) if total else None,
        "examples": examples,
        "suspect_sources": sorted(
            ({"source": k, "downvotes": c} for k, c in suspect.items()),
            key=lambda d: -d["downvotes"],
        )[:5],
        "last_vote": mine[0].get("at", "") if mine else "",
    }


# ============================================================
# PUBLIC API
# ============================================================

def record_vote(
    msg_id: str,
    verdict: str,
    *,
    question: str = "",
    answer: str = "",
    sources: Optional[List[str]] = None,
    comment: str = "",
    voter: str = "",
    conv_id: str = "",
) -> Optional[dict]:
    """
    Store one 👍/👎. Returns the updated topic aggregate, or None if the input was
    unusable.

    Keyed by `msg_id`, so voting again on the same message replaces the previous
    vote instead of adding a second one.

    Never raises: called from the chat UI path.
    """
    msg_id = (msg_id or "").strip()
    verdict = (verdict or "").strip().lower()
    if not msg_id or verdict not in VALID_VERDICTS:
        return None

    topic_key = normalize_question(question) or "(unclassified)"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        with _lock:
            store = _load()
            votes = store.setdefault("votes", {})

            votes[msg_id] = {
                "msg_id": msg_id,
                "verdict": verdict,
                "question": " ".join((question or "").split())[:200],
                "topic_key": topic_key,
                "answer_snippet": " ".join((answer or "").split())[:300],
                "sources": [s for s in (sources or []) if isinstance(s, str)][:8],
                "comment": " ".join((comment or "").split())[:300],
                "voter": voter,
                "conv_id": conv_id,
                "at": now,
            }

            store["votes"] = _prune_votes(votes)

            # Rebuild every topic touched by the surviving votes, so pruning can
            # never leave an aggregate describing votes that are gone.
            topics = {}
            for key in {v.get("topic_key", "") for v in store["votes"].values()}:
                if key:
                    topics[key] = _rebuild_topic(key, store["votes"])
            store["topics"] = topics

            _save(store)
            return topics.get(topic_key)
    except Exception as e:
        print(f"  [feedback] could not record vote (non-fatal): {e}")
        return None


def get_vote(msg_id: str) -> Optional[dict]:
    """The current vote for one message, if any — lets the UI show its state."""
    return _load().get("votes", {}).get((msg_id or "").strip())


def get_votes(msg_ids: List[str]) -> Dict[str, dict]:
    """
    Votes for many messages in one read.

    Reopening a conversation needs the verdict for every answer in it. Calling
    `get_vote()` per message would re-read and re-parse the whole store once per
    answer and fire one HTTP request each — for a twenty-message thread that is
    twenty round trips to restore two thumbs. This reads once.

    Missing ids are simply absent from the result rather than mapped to None, so
    the caller can treat the response as "the votes that exist".
    """
    votes = _load().get("votes", {})
    out: Dict[str, dict] = {}
    for mid in msg_ids or []:
        key = (mid or "").strip()
        if key and key in votes:
            out[key] = votes[key]
    return out



def list_topics(verdict: Optional[str] = None) -> List[dict]:
    """
    Topic aggregates.

    Default order is **worst first** (most downvotes), because the admin's
    question is "what is failing?", not "what is popular?".

    `verdict="down"` keeps only topics with at least one downvote; `"up"` keeps
    only topics with no downvotes at all (the known-good set worth protecting
    with regression tests).
    """
    topics = list(_load().get("topics", {}).values())

    if verdict == "down":
        topics = [t for t in topics if int(t.get("down", 0)) > 0]
    elif verdict == "up":
        topics = [t for t in topics if int(t.get("down", 0)) == 0 and int(t.get("up", 0)) > 0]

    topics.sort(key=lambda t: (-int(t.get("down", 0)), -int(t.get("total", 0))))
    return topics


def feedback_stats() -> dict:
    """Headline numbers for the dashboard."""
    votes = list(_load().get("votes", {}).values())
    up = sum(1 for v in votes if v.get("verdict") == "up")
    down = sum(1 for v in votes if v.get("verdict") == "down")
    total = up + down
    return {
        "total_votes": total,
        "up": up,
        "down": down,
        "satisfaction": round(up / total, 3) if total else None,
        "topics_with_downvotes": sum(
            1 for t in _load().get("topics", {}).values() if int(t.get("down", 0)) > 0
        ),
    }


def regression_questions(min_upvotes: int = 1) -> List[dict]:
    """
    Questions whose answers students approved, with the sources that produced
    them — a ready-made regression set for `eval_retrieval.py`.

    The point: after changing chunking, the reranker or the model, these must
    still retrieve the same documents. Without this, "did I break anything?" is
    answered by intuition.
    """
    out = []
    for t in _load().get("topics", {}).values():
        if int(t.get("up", 0)) < min_upvotes or int(t.get("down", 0)) > 0:
            continue
        for ex in t.get("examples", []):
            if ex.get("verdict") == "up" and ex.get("question"):
                out.append({
                    "question": ex["question"],
                    "expected_sources": ex.get("sources", []),
                    "topic_key": t.get("key", ""),
                })
    return out


def delete_topic(key: str) -> bool:
    """Drop a topic and its votes (test traffic, spam)."""
    with _lock:
        store = _load()
        votes = store.get("votes", {})
        doomed = [mid for mid, v in votes.items() if v.get("topic_key") == key]
        if not doomed and key not in store.get("topics", {}):
            return False
        for mid in doomed:
            del votes[mid]
        store.get("topics", {}).pop(key, None)
        store["votes"] = votes
        _save(store)
        return True
