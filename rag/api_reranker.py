# rag/api_reranker.py
"""
Hosted cross-encoder reranker over OpenRouter's `/api/v1/rerank` endpoint.

Why a second reranker
---------------------
`rag/reranker.py` runs a 32M cross-encoder on this box's CPU. That is the right
answer when the machine has cores to spare and the weights are already on disk,
and the wrong answer in two situations this project actually hits:

  * the deployment target is a 2-vCPU box, and reranking is the ONLY CPU-bound
    phase of a request (docs/CAPACITY.md). It is what capped concurrency at 2-4
    simultaneous askers, which is why `RERANKER_ENABLED` defaults to false and
    why ordering quality is currently being given up entirely.

  * a fresh container without HuggingFace egress cannot fetch the weights at all,
    so the local path degrades to raw retrieval order and says so only in a log
    line nobody is reading.

This module trades those CPU seconds for a network round-trip. The model is a 1B
reranker — roughly 30x the parameters of the local one — served by NVIDIA through
OpenRouter's free tier, so the exchange is "more accurate ordering, no local CPU,
but now you need egress and you are inside someone else's rate limit".

Neither backend is universally better, which is exactly why the choice is a
runtime setting (`rag/rerank_settings.py`) rather than a constant here.

The endpoint
------------
    POST https://openrouter.ai/api/v1/rerank
    {"model": ..., "query": ..., "documents": [...], "top_n": N}

    -> {"results": [{"index": 1, "relevance_score": 0.178,
                     "document": {"text": "..."}}, ...]}

`index` is the position in the `documents` array that was sent, and that is the
only field this module trusts for identity. The response is ordered by score and
may be SHORTER than the input (the endpoint honours `top_n`), so matching results
back to documents by position in the response — rather than by `index` — would
silently attach one document's score to a different document. Every mapping below
goes through `index`.

Scores are 0..1 relevance, not the local cross-encoder's unbounded logits. They
are never compared against each other across backends; each is only used to sort
within one request, so the different ranges do not need reconciling.

Design constraints, identical to the local reranker
---------------------------------------------------
1. **Never break the app.** A missing key, a 429, a timeout, or a malformed
   payload all degrade to the original retrieval order. The student gets a
   worse-ordered answer, never an error.
2. **Bounded work.** One HTTP call per (query phrasing) rather than per document,
   with a hard timeout and a capped candidate count.
3. **No document-specific knowledge.** Everything here is a property of text.
4. **Same public shape as `rag/reranker.py`** — `rerank()`, `rerank_multi()`,
   `available()`, `unavailable_reason()` — so `rag/chain.py` can switch between
   them by name and nothing downstream has to care which one ran.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# The hosted model. `:free` is part of the id, not a decoration — it selects the
# free-tier routing on OpenRouter, and dropping it silently starts billing.
DEFAULT_API_RERANKER_MODEL = "nvidia/llama-nemotron-rerank-vl-1b-v2:free"
API_RERANKER_MODEL = os.getenv("API_RERANKER_MODEL", DEFAULT_API_RERANKER_MODEL)

# Overridable so a self-hosted or proxied gateway can be pointed at without a
# code change; the path (`/rerank`) is appended below.
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")
RERANK_ENDPOINT = f"{OPENROUTER_BASE_URL}/rerank"


# ---------------------------------------------------------------------------
# Timeout: why it is short, and why a timeout is not an error
# ---------------------------------------------------------------------------
# This call sits between the student pressing Enter and the first token of their
# answer, while `rag/progress.py` is showing "Reading the most relevant pages".
# Every second here is a second of spinner.
#
# The whole point of the API backend is that it is FASTER than ~3.5 s of
# contended local CPU. A generous timeout would destroy that: a request that
# takes 20 s to rank 40 short passages is not going to produce a useful answer
# late, it is going to produce the same answer after the student has given up. So
# the deadline is tight, and blowing it degrades to retrieval order — the exact
# same fallback as a missing local model.
API_TIMEOUT = float(os.getenv("API_RERANKER_TIMEOUT", "12"))

# Retries are for TRANSIENT failures only (see `_should_retry`). A 401 retried
# three times is three times the latency and the same 401.
API_MAX_RETRIES = max(0, int(os.getenv("API_RERANKER_RETRIES", "1")))

# Candidates sent per call. Mirrors MAX_PAIRS in the local reranker so the two
# backends are judged on the same pool, and OpenRouter's documented `top_n`
# ceiling is 100, so this stays comfortably inside it.
MAX_DOCS = int(os.getenv("API_RERANKER_MAX_DOCS", "40"))

# Characters of each document actually sent.
#
# The local reranker windows a chunk into ~900-char passages and keeps the best
# window, because one score for a 3000-char chunk is an average over everything
# in it. That trick costs nothing locally (more windows = more batched rows) but
# would cost a multiple of the REQUEST here, so this backend sends one truncated
# passage per document instead.
#
# 2000 rather than 900: a hosted 1B model has a far longer context than the 32M
# local one, so the window that exists to protect a small model's attention is
# not needed — and a longer passage means the answer is less likely to fall off
# the end of the truncation.
MAX_DOC_CHARS = int(os.getenv("API_RERANKER_MAX_DOC_CHARS", "2000"))

# Phrasings scored per question. `rerank()` is called with the student's natural
# question plus keyword rewrites, and each phrasing is its own HTTP call, so this
# is the real latency multiplier. 2 keeps the interrogative form AND one lexical
# rewrite — the pair that the local reranker's docstring shows is what decides
# whether "who is the dean of CITAS?" finds a name or a history paragraph.
MAX_QUERIES = max(1, int(os.getenv("API_RERANKER_MAX_QUERIES", "2")))


# ---------------------------------------------------------------------------
# Concurrency: bounded for a different reason than the local reranker
# ---------------------------------------------------------------------------
# The local semaphore exists because CPU is finite. This one exists because the
# free tier's rate limit is finite: 20 questions arriving together would fire 20+
# requests at OpenRouter, and the answer to most of them would be 429. A queue
# converts "everyone gets rate-limited" into "the first few are ranked and the
# rest wait a moment", which is strictly better for the median student.
#
# Higher than the local limit (2) because these slots are held during a network
# wait, not during computation — an idle thread blocked on a socket is not
# competing for the same resource a cross-encoder batch is.
API_CONCURRENCY = max(1, int(os.getenv("API_RERANKER_CONCURRENCY", "6")))
API_QUEUE_TIMEOUT = float(os.getenv("API_RERANKER_QUEUE_TIMEOUT", "10"))

_slots = threading.BoundedSemaphore(API_CONCURRENCY)

# Why the last call failed, for `unavailable_reason()`. A silent degradation has
# to be explainable: "no score on any document" reads identically whether the key
# is missing, the quota is spent, or the network is down.
_last_error = ""
_error_lock = threading.Lock()


def _api_key() -> str:
    """
    The key, read at CALL time rather than import time.

    Deliberate: `config.py` loads `.env`, and the import order between this
    module and config is not this file's to guarantee. Reading late means a key
    that arrives through either path is found, and that a key added without a
    restart is picked up.
    """
    key = os.getenv("OPENROUTER_API_KEY", "")
    if key:
        return key.strip()
    try:
        from config import OPENROUTER_API_KEY

        return (OPENROUTER_API_KEY or "").strip()
    except Exception:
        return ""


def configured() -> bool:
    """True when a key exists. Does NOT prove the key works — only a call can."""
    return bool(_api_key())


def available() -> bool:
    """
    True when this backend can be used at all.

    The mirror of `rag.reranker.available()`, which is what lets `rag/chain.py`
    log "reranked" vs "NOT reranked" without knowing which backend ran.
    """
    return configured()


def unavailable_reason() -> str:
    """Why the last attempt produced no scores, or "" when all is well."""
    if not configured():
        return "OPENROUTER_API_KEY is not set"
    with _error_lock:
        return _last_error


def _remember_error(msg: str) -> None:
    global _last_error
    with _error_lock:
        _last_error = msg


def _clear_error() -> None:
    global _last_error
    with _error_lock:
        _last_error = ""


def warmup() -> bool:
    """
    Present so this module is drop-in compatible with `rag.reranker`.

    There is nothing to load — no weights, no torch — so this only reports
    whether a key is configured. It deliberately does NOT make a probe request:
    a warmup that spends a free-tier call on every worker boot is a warmup that
    makes the first real question more likely to be rate-limited.
    """
    return configured()


def disabled() -> bool:
    """
    False, always.

    `rag.reranker` has an off switch because it costs CPU that the box may not
    have. This one costs no local CPU, so "switched off" is expressed by choosing
    the other backend in `rag/rerank_settings.py`, not by a second flag that
    could contradict it.
    """
    return False


def _header_of(doc) -> str:
    """
    The section heading, prepended to the passage.

    'ENROLLMENT PROCEDURE' or 'COURSE OFFERING' is a strong relevance signal that
    the body text may not repeat, and it is the same signal the local reranker
    puts in front of every window — kept identical so a backend switch does not
    change what the model is looking at.
    """
    md = getattr(doc, "metadata", None) or {}
    return " ".join(str(md[k]) for k in ("section", "header") if md.get(k)).strip()


def _passage_of(doc) -> str:
    """One passage per document: heading + truncated body."""
    body = (getattr(doc, "page_content", "") or "").strip()
    header = _header_of(doc)

    if len(body) > MAX_DOC_CHARS:
        body = body[:MAX_DOC_CHARS]

    if header and body:
        return f"{header}\n{body}"
    return header or body


def _should_retry(status: Optional[int]) -> bool:
    """
    Transient failures only.

    429 (rate limited) and 5xx are worth one more attempt; 400/401/403/404 mean
    the request itself is wrong, and repeating it just spends latency to receive
    the same rejection.
    """
    if status is None:      # socket error / timeout — the request never landed
        return True
    return status == 429 or 500 <= status < 600


def _post(payload: dict) -> Optional[dict]:
    """
    One rerank call. Returns the decoded body, or None on any failure.

    urllib rather than `requests`: `requests` is not in requirements.txt, and
    adding a dependency for one POST to one endpoint is not a trade worth making
    in an image that already carries torch.

    Never raises. Every exit is either a dict or None, because the only caller is
    on the path that answers a student's question.
    """
    key = _api_key()
    if not key:
        _remember_error("OPENROUTER_API_KEY is not set")
        return None

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # OpenRouter attributes traffic with these. Optional, but they are what
        # make this app identifiable in the dashboard when a quota is being
        # investigated.
        "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://samarcolleges.edu.ph"),
        "X-Title": os.getenv("OPENROUTER_SITE_NAME", "SC Assistant"),
    }

    attempts = API_MAX_RETRIES + 1
    for attempt in range(attempts):
        req = urllib.request.Request(
            RERANK_ENDPOINT, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _clear_error()
            return data

        except urllib.error.HTTPError as e:
            # Read the body: OpenRouter puts the actionable part ("rate limit
            # exceeded", "model not found") in there, and a bare status code
            # sends whoever reads the log to the wrong problem.
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            msg = f"HTTP {e.code}: {detail or e.reason}"
            if _should_retry(e.code) and attempt < attempts - 1:
                logger.warning("API rerank %s, retrying", msg)
                continue
            _remember_error(msg)
            logger.warning("API rerank failed: %s", msg)
            print(f"  API rerank failed (non-fatal): {msg}")
            return None

        except Exception as e:  # timeout, DNS, TLS, malformed JSON...
            msg = f"{type(e).__name__}: {e}"
            if attempt < attempts - 1:
                logger.warning("API rerank %s, retrying", msg)
                continue
            _remember_error(msg)
            logger.warning("API rerank failed: %s", msg)
            print(f"  API rerank failed (non-fatal): {msg}")
            return None

    return None


def _score_one(query: str, passages: Sequence[str]) -> Optional[List[float]]:
    """
    Score every passage against ONE query. Returns a score per passage, aligned
    with `passages` by position, or None when the call could not be made.

    `top_n=len(passages)` because this is a RANKING step inside a larger pipeline,
    not the final selection: `rag/chain.py` decides how many documents reach the
    LLM (FINAL_TOP_K), and truncating here would throw away documents that the
    caller's own top_k was still going to keep.

    Holds a concurrency slot for the duration. On timeout the return is None —
    "too busy", which the caller treats as "answer from retrieval order" rather
    than making a student wait behind a queue for ranking they will not notice.
    """
    if not passages:
        return None

    acquired = _slots.acquire(timeout=API_QUEUE_TIMEOUT)
    if not acquired:
        msg = f"no slot within {API_QUEUE_TIMEOUT:.0f}s (concurrency={API_CONCURRENCY})"
        _remember_error(msg)
        logger.warning("API reranker busy: %s, answering from retrieval order", msg)
        print(f"  API reranker busy ({msg}), keeping retrieval order")
        return None

    try:
        data = _post({
            "model": API_RERANKER_MODEL,
            "query": query,
            "documents": list(passages),
            "top_n": len(passages),
            # The passages were built here and are already held in memory;
            # echoing every one of them back doubles the response for no gain.
            "return_documents": False,
        })
    finally:
        # `finally`, not a call after the post: an exception escaping with a slot
        # held would permanently shrink capacity, and after API_CONCURRENCY of
        # them this backend would be off for the life of the process with no log
        # line saying so.
        _slots.release()

    if not isinstance(data, dict):
        return None

    results = data.get("results")
    if not isinstance(results, list):
        _remember_error(f"unexpected response shape: {str(data)[:200]}")
        logger.warning("API rerank returned no results array")
        return None

    # Missing entries keep -inf, so a document the endpoint declined to score
    # sorts below every scored one instead of being promoted by a default 0.
    scores = [float("-inf")] * len(passages)
    for item in results:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        # `index` is the position in the array we SENT. The results list is
        # ordered by score and may be shorter than the input, so reading it
        # positionally would attach one document's score to another.
        if isinstance(idx, int) and 0 <= idx < len(passages) and score is not None:
            try:
                scores[idx] = float(score)
            except (TypeError, ValueError):
                continue

    if all(s == float("-inf") for s in scores):
        _remember_error("no usable scores in response")
        return None

    return scores


def rerank(
    query: str,
    docs: Sequence,
    top_k: Optional[int] = None,
    max_pairs: int = MAX_DOCS,
    queries: Optional[Sequence[str]] = None,
) -> List:
    """
    Reorder `docs` by hosted cross-encoder relevance to `query`.

    Signature-compatible with `rag.reranker.rerank()` — same arguments, same
    fallback contract (returns the original order, never raises), same
    `metadata['rerank_score']` on the way out — because `rag/chain.py` picks one
    of the two at call time and nothing downstream should be able to tell which
    ran.

    Multi-query scoring works the same way too: a document's score is the MAX
    across phrasings, because a keyword rewrite and a natural question match
    different passage shapes and the best of the two is the honest answer. The
    difference is cost — locally the phrasings are extra rows in one batch, here
    each is its own HTTP call — so the number of phrasings is capped at
    MAX_QUERIES rather than unbounded.

    Args:
        query:     primary search query
        docs:      retrieved LangChain Documents
        top_k:     truncate the reranked list to this many docs (None = all)
        max_pairs: only score this many candidates (the rest keep their order
                   and are appended after the scored ones)
        queries:   optional additional phrasings; `query` is always included
    """
    if not docs:
        return []

    all_queries: List[str] = []
    for q in [query, *(queries or [])]:
        q = (q or "").strip()
        if q and q not in all_queries:
            all_queries.append(q)
    all_queries = all_queries[:MAX_QUERIES]

    if not all_queries or not configured():
        if not configured():
            _remember_error("OPENROUTER_API_KEY is not set")
        return list(docs)[:top_k] if top_k else list(docs)

    head = list(docs[:max_pairs])
    tail = list(docs[max_pairs:])

    passages = [_passage_of(d) for d in head]
    if not any(p.strip() for p in passages):
        return list(docs)[:top_k] if top_k else list(docs)

    best = [float("-inf")] * len(head)
    scored_any = False

    for q in all_queries:
        scores = _score_one(q, passages)
        if scores is None:
            # One phrasing failing is not the request failing: if an earlier
            # phrasing already produced scores, those are still a better ordering
            # than the retriever's.
            continue
        scored_any = True
        for i, s in enumerate(scores):
            if s > best[i]:
                best[i] = s

    if not scored_any:
        return list(docs)[:top_k] if top_k else list(docs)

    for doc, score in zip(head, best):
        try:
            if doc.metadata is None:
                doc.metadata = {}
            doc.metadata["rerank_score"] = score if score != float("-inf") else 0.0
        except Exception:
            pass  # metadata is optional; never fail the request over it

    # Stable sort keeps the retriever's order as the tie-breaker.
    order = sorted(range(len(head)), key=lambda i: best[i], reverse=True)
    ranked = [head[i] for i in order]
    ranked.extend(tail)  # unscored candidates stay last

    return ranked[:top_k] if top_k else ranked


def rerank_multi(
    aspects: Sequence[str],
    docs: Sequence,
    top_k: Optional[int] = None,
    max_pairs: int = MAX_DOCS,
) -> List:
    """
    Rank for a question that asks about SEVERAL things at once, guaranteeing
    every part gets representation in the final list.

    Same round-robin merge as `rag.reranker.rerank_multi()`, and for the same
    measured reason: a single score per document silently becomes majority rule,
    so a three-part question where two parts are about principals pushed the
    CITAS evidence to rank 13 — one slot past the cutoff — even though that
    document ranked #1 for its own sub-question. Taking each aspect's best
    remaining document in turn gives every ask roughly K/N slots, so no part can
    be starved by another.

    The cost model differs from the local version. There, all (aspect, window)
    pairs go into ONE batched predict() call, so the fix for "48 s on a 3-part
    question" was batching. Here each aspect is an HTTP request, so the cap is
    the number of aspects: MAX_QUERIES requests, sequential, each bounded by
    API_TIMEOUT.

    Args:
        aspects:   query strings, one per part of the question (order = priority)
        docs:      retrieved LangChain Documents
        top_k:     truncate the merged list to this many docs (None = all)
        max_pairs: only score this many candidates per aspect
    """
    if not docs:
        return []

    clean: List[str] = []
    for a in aspects or []:
        a = (a or "").strip()
        if a and a not in clean:
            clean.append(a)

    if not clean:
        return list(docs)[:top_k] if top_k else list(docs)

    # Single aspect -> ordinary rerank (no merging needed).
    if len(clean) == 1:
        return rerank(clean[0], docs, top_k=top_k, max_pairs=max_pairs)

    if not configured():
        _remember_error("OPENROUTER_API_KEY is not set")
        return list(docs)[:top_k] if top_k else list(docs)

    # Each aspect is a separate network round-trip, so this cap is a latency
    # budget, not a quality preference. Aspects arrive in priority order from
    # `split_question()`, so truncating takes the least important parts.
    clean = clean[:MAX_QUERIES]

    head = list(docs[:max_pairs])
    tail = list(docs[max_pairs:])
    passages = [_passage_of(d) for d in head]

    def key_of(doc):
        md = getattr(doc, "metadata", None) or {}
        return md.get("chunk_id") or (
            md.get("source"), md.get("page"), (doc.page_content or "")[:120]
        )

    per_aspect_scores: List[List[float]] = []
    kept_aspects: List[str] = []
    for aspect in clean:
        scores = _score_one(aspect, passages)
        if scores is None:
            continue
        per_aspect_scores.append(scores)
        kept_aspects.append(aspect)

    if not per_aspect_scores:
        return list(docs)[:top_k] if top_k else list(docs)

    # Each aspect's own ranking of the pool.
    per_aspect = []
    for scores in per_aspect_scores:
        order = sorted(range(len(head)), key=lambda i: scores[i], reverse=True)
        per_aspect.append([head[i] for i in order])

    best_score = {}
    for d_i, doc in enumerate(head):
        top = max(s[d_i] for s in per_aspect_scores)
        best_score[key_of(doc)] = top if top != float("-inf") else 0.0

    # Round-robin merge: aspect 1's best, aspect 2's best, ... then seconds, etc.
    merged: List = []
    seen = set()
    cursors = [0] * len(per_aspect)

    while len(merged) < len(head):
        progressed = False
        for a_i, ranking in enumerate(per_aspect):
            while cursors[a_i] < len(ranking):
                doc = ranking[cursors[a_i]]
                cursors[a_i] += 1
                k = key_of(doc)
                if k in seen:
                    continue
                seen.add(k)
                try:
                    if doc.metadata is None:
                        doc.metadata = {}
                    doc.metadata["rerank_score"] = best_score.get(k, 0.0)
                    doc.metadata["rerank_aspect"] = kept_aspects[a_i]
                except Exception:
                    pass
                merged.append(doc)
                progressed = True
                break
            if top_k and len(merged) >= top_k:
                break
        if not progressed:
            break

    merged.extend(tail)  # unscored candidates stay last
    return merged[:top_k] if top_k else merged
