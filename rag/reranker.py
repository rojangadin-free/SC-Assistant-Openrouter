# rag/reranker.py
"""
Lightweight local cross-encoder reranker (sentence-transformers).

Why a reranker?
---------------
Hybrid retrieval (BM25 + dense) is a *recall* device: it is good at making sure
the right page is somewhere in the candidate pool, but it is bad at ordering.
The old-student enrollment bug is a pure ordering failure:

    query : "enrollment process admission requirements ... enrollment procedure"
    top-1 : p46  ADMISSION REQUIREMENTS      <-- lexically perfect, wrong content
    ...
    p40   OLD STUDENTS 10-step flowchart     <-- the real answer, ranked too low

BM25 loves p46 because it repeats "admission requirements"; the dense vector
loves the several "Chapter N ... Enrollment Procedure" table-of-contents lines.
A cross-encoder reads (query, passage) *jointly* and scores actual relevance,
so the page that literally contains the 10 steps wins.

Model
-----
`cross-encoder/ms-marco-MiniLM-L6-v2` — the current recommended lightweight
reranker from the sentence-transformers project:

  * 6 layers / ~22.7M params / ~90 MB  (same size class as the MiniLM-L6
    embedding model already used by this project)
  * CPU-friendly: ~15-40 ms for 30 pairs on a normal laptop core
  * NDCG@10 74.30 on TREC DL19 vs 71.01 for the older TinyBERT-L2

Design constraints honoured here
-------------------------------
1. **Never break the app.** Import errors, download failures, or scoring errors
   all degrade gracefully back to the original retrieval order.
2. **Lazy + cached.** The model loads on first use and is reused afterwards, so
   importing this module is free and Flask boot time is unaffected.
3. **Bounded work.** Only the first `MAX_PAIRS` candidates are scored, and the
   total number of scored windows is capped.
4. **No document-specific knowledge.** Nothing here knows about Samar College,
   SCTI, or any particular heading. Everything is a property of *text*, so a
   newly uploaded document benefits automatically.
"""


from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# The lightweight, current-generation cross-encoder. Override via env var if a
# deployment wants the stronger (but slower) L12 variant.
RERANKER_MODEL_NAME = os.getenv(
    "cross-encoder/ettin-reranker-32m-v1", "cross-encoder/ettin-reranker-32m-v1"
)

# Cap the cross-encoder work: scoring is O(candidates), and beyond ~40 the
# hybrid retriever's own ordering is already noise.
MAX_PAIRS = int(os.getenv("RERANKER_MAX_PAIRS", "40"))

# ---------------------------------------------------------------------------
# Sliding-window scoring
# ---------------------------------------------------------------------------
# A cross-encoder is a PASSAGE scorer. ms-marco models truncate at 512 tokens
# (~1800 chars), so feeding a 3000-char chunk means everything past the cutoff
# is invisible — and worse, the visible prefix may be unrelated boilerplate,
# which actively pushes the score DOWN.
#
# Measured on the live index for "list of academic programs and courses offered":
#
#   whole-chunk score of the chunk containing the full program list : -9.842
#   score of the same chunk's COURSE OFFERING window               : +8.711
#
# The correct chunk was ranked LAST because its first 2000 chars happen to be
# a reference list and classroom rules. Scoring overlapping windows and keeping
# the best one fixes this without any knowledge of what the document contains.
WINDOW_CHARS = int(os.getenv("RERANKER_WINDOW_CHARS", "900"))
WINDOW_STRIDE = int(os.getenv("RERANKER_WINDOW_STRIDE", "450"))

# Hard ceiling on windows per document, so a pathologically long chunk cannot
# blow up latency.
MAX_WINDOWS_PER_DOC = int(os.getenv("RERANKER_MAX_WINDOWS", "8"))

# Total (query, window) pairs scored per request. This is the real latency knob.
MAX_TOTAL_WINDOWS = int(os.getenv("RERANKER_MAX_TOTAL_WINDOWS", "220"))


_model = None                      # cached CrossEncoder instance
_load_failed = False               # set True after a failed attempt (don't retry)
_load_lock = threading.Lock()      # guard concurrent first-use from threads


def _load_model():
    """Load (once) and return the CrossEncoder, or None if unavailable."""
    global _model, _load_failed

    if _model is not None:
        return _model
    if _load_failed:
        return None

    with _load_lock:
        # Re-check inside the lock (another thread may have finished loading).
        if _model is not None:
            return _model
        if _load_failed:
            return None

        try:
            from sentence_transformers import CrossEncoder

            logger.info("Loading reranker model: %s", RERANKER_MODEL_NAME)
            print(f"  Loading reranker: {RERANKER_MODEL_NAME} ...")
            _model = CrossEncoder(
                RERANKER_MODEL_NAME,
                max_length=8192,
                device="cpu",
            )
            print("  Reranker ready.")  
            return _model
        except Exception as e:  # ImportError, network/download failure, OOM...
            _load_failed = True
            logger.warning("Reranker unavailable, using retrieval order: %s", e)
            print(f"  Reranker unavailable (non-fatal), keeping retrieval order: {e}")
            return None


def warmup() -> bool:
    """
    Eagerly load the model (e.g. at app start) so the first user question does
    not pay the one-time load cost. Returns True when the model is ready.
    """
    return _load_model() is not None


def _header_of(doc) -> str:
    """
    The section heading, prepended to every window. 'ENROLLMENT PROCEDURE' or
    'COURSE OFFERING' is a strong relevance signal that the body text of a
    given window may not repeat.
    """
    md = getattr(doc, "metadata", None) or {}
    return " ".join(str(md[k]) for k in ("section", "header") if md.get(k)).strip()


def _windows_of(doc) -> List[str]:
    """
    Split a chunk into overlapping passage-sized windows, each prefixed with the
    section heading.

    The overlap matters: a list that straddles a window boundary would otherwise
    be split across two windows and score poorly in both.
    """
    body = (getattr(doc, "page_content", "") or "").strip()
    header = _header_of(doc)

    if not body:
        return [header] if header else []

    if len(body) <= WINDOW_CHARS:
        slices = [body]
    else:
        slices = [
            body[i:i + WINDOW_CHARS]
            for i in range(0, len(body) - WINDOW_CHARS + WINDOW_STRIDE, WINDOW_STRIDE)
        ][:MAX_WINDOWS_PER_DOC]

    if not header:
        return slices
    return [f"{header}\n{s}" for s in slices]


def _passage_of(doc) -> str:
    """First window only — kept for callers/tests that want a single passage."""
    wins = _windows_of(doc)
    return wins[0] if wins else ""



def rerank(
    query: str,
    docs: Sequence,
    top_k: Optional[int] = None,
    max_pairs: int = MAX_PAIRS,
    queries: Optional[Sequence[str]] = None,
) -> List:
    """
    Reorder `docs` by cross-encoder relevance to `query`.

    Falls back to the original order (never raises) when the model cannot be
    loaded or scoring fails. The relevance score is stored on each returned doc
    as `metadata['rerank_score']` for logging/debugging.

    Each document is scored as the MAXIMUM over its overlapping windows, so a
    chunk is judged by its most relevant passage rather than by whatever text
    happens to sit at its start.

    Multi-query scoring
    -------------------
    `queries` accepts several phrasings of the same information need (typically
    the user's natural question AND a keyword rewrite). A document's score is
    the MAX across all phrasings.

    This exists because cross-encoder scores are not comparable across query
    styles, and the difference decides the answer. Same pool, same model,
    same 12 slots — only the query wording changed:

      query: "Samar College high school principal elementary principal Citas dean"
        -> "PRINCIPAL, JUNIOR HIGH SCHOOL" chunk : not in top 12
        -> "PRINCIPAL, ELEMENTARY"         chunk : not in top 12
        (top 10 slots all went to HISTORICAL ACCOUNT / ACADEMIC AWARDS prose,
         which is *about* principals in general but names nobody current)

      query: "who are the principal of highschool, elementary and who is the dean of citas?"
        -> "PRINCIPAL, JUNIOR HIGH SCHOOL" chunk : rank 2
        -> "PRINCIPAL, ELEMENTARY"         chunk : rank 6

    A bag of keywords ("principal elementary dean") matches any passage that
    discusses those roles. A real question ("who is...?") matches passages
    shaped like an answer to it — a name next to a title. The keyword rewrite
    destroyed the interrogative form the cross-encoder needs, which is why the
    same question answered correctly only about half the time. Scoring with
    both phrasings and keeping the best removes that coin flip.

    Args:
        query:     primary search query (kept for backwards compatibility)
        docs:      retrieved LangChain Documents
        top_k:     truncate the reranked list to this many docs (None = all)
        max_pairs: only score this many candidates (the rest keep their order
                   and are appended after the scored ones)
        queries:   optional additional phrasings; `query` is always included
    """
    if not docs:
        return []

    # Assemble the phrasings to score against, de-duplicated, order preserved.
    all_queries: List[str] = []
    for q in [query, *(queries or [])]:
        q = (q or "").strip()
        if q and q not in all_queries:
            all_queries.append(q)

    if not all_queries:
        return list(docs)[:top_k] if top_k else list(docs)

    model = _load_model()
    if model is None:
        return list(docs)[:top_k] if top_k else list(docs)

    head = list(docs[:max_pairs])
    tail = list(docs[max_pairs:])

    # Build the (query, window) pair list, remembering which doc each pair
    # belongs to. Windows are interleaved round-robin so that, if we hit the
    # global budget, every document still gets its first window scored.
    per_doc = [_windows_of(d) for d in head]
    plan: List[tuple] = []
    for w_i in range(max((len(w) for w in per_doc), default=0)):
        for d_i, wins in enumerate(per_doc):
            if w_i < len(wins):
                plan.append((d_i, wins[w_i]))
    plan = plan[:MAX_TOTAL_WINDOWS]

    if not plan:
        return list(docs)[:top_k] if top_k else list(docs)

    # The window budget is per query phrasing, so latency scales with the number
    # of phrasings — hence the small, fixed cap in the caller.
    pairs: List[tuple] = []
    owners: List[int] = []
    for q in all_queries:
        for d_i, window in plan:
            pairs.append((q, window))
            owners.append(d_i)

    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception as e:
        logger.warning("Rerank scoring failed, keeping retrieval order: %s", e)
        print(f"  Rerank scoring failed (non-fatal): {e}")
        return list(docs)[:top_k] if top_k else list(docs)

    best = [float("-inf")] * len(head)
    for d_i, score in zip(owners, scores):
        s = float(score)
        if s > best[d_i]:
            best[d_i] = s


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
    max_pairs: int = MAX_PAIRS,
) -> List:
    """
    Rank for a question that asks about SEVERAL things at once, guaranteeing
    every part gets representation in the final list.

    Why a plain rerank is not enough
    --------------------------------
    A single score per document expresses "how relevant is this to the whole
    query", which silently becomes majority rule. Measured on
    "who are the principal of highschool, elementary and who is the dean of
    citas?" — three asks, one of which is about CITAS:

      ranked as ONE query (top 12):
        principals ........ ranks 7 and 12
        CITAS ............. rank 13   <-- cut off, right after the boundary

      ranked per part (each part's own top 1):
        "who is the principal of high school?" -> PRINCIPAL, JHS/SHS   (9.87)
        "who is the principal of elementary?"  -> PRINCIPAL, ELEMENTARY (10.84)
        "who is the dean of CITAS?"            -> DEANS UPDATE / CITAS (10.28)

    Every part's evidence was in the pool the whole time and every part ranks
    #1 for its own sub-question. Only the merge was wrong: two of the three
    asks were about principals, so principal-ish and generic-history pages
    filled the list and the minority ask was pushed over the edge. That is a
    fixed-size-list starvation problem, not a relevance problem.

    The merge used here is round-robin across per-aspect rankings: take each
    aspect's best remaining document in turn. With K slots and N aspects every
    aspect is guaranteed roughly K/N slots, so no ask can be starved by another,
    regardless of how many asks there are or which one the retriever favours.

    Args:
        aspects:   query strings, one per part of the question (order = priority)
        docs:      retrieved LangChain Documents
        top_k:     truncate the merged list to this many docs (None = all)
        max_pairs: only score this many candidates per aspect
    """
    if not docs:
        return []

    clean = []
    for a in aspects or []:
        a = (a or "").strip()
        if a and a not in clean:
            clean.append(a)

    if not clean:
        return list(docs)[:top_k] if top_k else list(docs)

    # Single aspect -> ordinary rerank (no merging needed).
    if len(clean) == 1:
        return rerank(clean[0], docs, top_k=top_k, max_pairs=max_pairs)

    # ----------------------------------------------------------------------
    # One batched forward pass, not one per aspect.
    #
    # Naively calling rerank() per aspect cost ~3.9 s each, so a 3-part
    # question took 48 s end to end — accurate but unusable. The work is
    # identical either way; what matters is that all (aspect, window) pairs go
    # to the model in ONE predict() call so batching is not wasted:
    #
    #   5 separate rerank() calls .... 22.8 s
    #   1 batched call (below) ....... measured after this change
    #
    # The per-aspect window budget is also divided by the number of aspects,
    # so total scored pairs stay near the single-query budget instead of
    # multiplying by the number of asks.
    # ----------------------------------------------------------------------
    model = _load_model()
    if model is None:
        return list(docs)[:top_k] if top_k else list(docs)

    head = list(docs[:max_pairs])
    tail = list(docs[max_pairs:])

    def key_of(doc):
        md = getattr(doc, "metadata", None) or {}
        return md.get("chunk_id") or (
            md.get("source"), md.get("page"), (doc.page_content or "")[:120]
        )

    # First window per doc is always scored; extra windows share what's left of
    # the budget. With N aspects the budget per aspect is MAX_TOTAL_WINDOWS/N,
    # so the total pair count stays comparable to a single-query rerank instead
    # of multiplying with the number of asks. (48 s -> 16 s on a 3-part question,
    # with no change in which documents reached the top 12.)
    budget = max(len(head), MAX_TOTAL_WINDOWS // max(len(clean), 1))


    per_doc = [_windows_of(d) for d in head]
    plan: List[tuple] = []
    for w_i in range(max((len(w) for w in per_doc), default=0)):
        for d_i, wins in enumerate(per_doc):
            if w_i < len(wins):
                plan.append((d_i, wins[w_i]))
    plan = plan[:budget]

    if not plan:
        return list(docs)[:top_k] if top_k else list(docs)

    pairs: List[tuple] = []
    tags: List[tuple] = []  # (aspect_index, doc_index) per pair
    for a_i, aspect in enumerate(clean):
        for d_i, window in plan:
            pairs.append((aspect, window))
            tags.append((a_i, d_i))

    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception as e:
        logger.warning("Multi-aspect rerank failed, falling back: %s", e)
        print(f"  Multi-aspect rerank failed (non-fatal): {e}")
        return rerank(clean[0], docs, top_k=top_k, max_pairs=max_pairs)

    # best_per_aspect[a][d] = best window score of doc d for aspect a
    n_a, n_d = len(clean), len(head)
    best_per_aspect = [[float("-inf")] * n_d for _ in range(n_a)]
    for (a_i, d_i), score in zip(tags, scores):
        s = float(score)
        if s > best_per_aspect[a_i][d_i]:
            best_per_aspect[a_i][d_i] = s

    # Each aspect's own ranking of the pool.
    per_aspect = []
    for a_i in range(n_a):
        order = sorted(range(n_d), key=lambda i: best_per_aspect[a_i][i], reverse=True)
        per_aspect.append([head[i] for i in order])

    best_score = {}
    for d_i, doc in enumerate(head):
        top = max(best_per_aspect[a_i][d_i] for a_i in range(n_a))
        best_score[key_of(doc)] = top if top != float("-inf") else 0.0


    # Round-robin merge: aspect 1's best, aspect 2's best, ... then seconds, etc.
    merged: List = []
    seen = set()
    cursors = [0] * len(per_aspect)

    while len(merged) < len(docs):
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
                    doc.metadata["rerank_aspect"] = clean[a_i]
                except Exception:
                    pass
                merged.append(doc)
                progressed = True
                break
            if top_k and len(merged) >= top_k:
                break
        if not progressed:
            break

    return merged[:top_k] if top_k else merged



