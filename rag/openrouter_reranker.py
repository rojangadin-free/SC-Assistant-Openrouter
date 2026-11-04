"""Opt-in, text-only OpenRouter reranking; no local model or startup requests.

NVIDIA's free endpoint logs inputs and prohibits personal/confidential data.
Only the query and selected document text are sent, but those can themselves
contain sensitive data. This adapter is NOT a PII-redaction mechanism.
"""

import copy
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from rag.runtime import env_int

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "cohere/rerank-5-pro"
MODEL_NAME = os.getenv("OPENROUTER_RERANKER_MODEL_NAME", DEFAULT_MODEL).strip()
ENDPOINT = "https://openrouter.ai/api/v1/rerank"
TIMEOUT_SECONDS = env_int("OPENROUTER_RERANKER_TIMEOUT_SECONDS", 6, maximum=30)
CONCURRENCY = env_int("OPENROUTER_RERANKER_CONCURRENCY", 4, maximum=12)
MAX_DOCUMENTS = env_int("OPENROUTER_RERANKER_MAX_DOCUMENTS", 40, maximum=100)
MAX_QUERIES = env_int("OPENROUTER_RERANKER_MAX_QUERIES", 4, maximum=4)
MAX_DOCUMENT_CHARS = env_int("OPENROUTER_RERANKER_DOCUMENT_CHARS", 6000, maximum=8000)
MAX_RESPONSE_BYTES = 1024 * 1024

_slots = threading.BoundedSemaphore(CONCURRENCY)
_client = None
_client_lock = threading.Lock()
_cooldown_lock = threading.Lock()
_cooldown_until = 0.0
_status = threading.local()


class RerankFailure(Exception):
    """A safe diagnostic without an API key, query, document, or response body."""


def configuration_error():
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        return "OPENROUTER_API_KEY is not set"
    if not MODEL_NAME:
        return "OPENROUTER_RERANKER_MODEL_NAME is empty"
    return ""


def available():
    """Whether this thread's most recent rerank succeeded, not a health probe."""
    return getattr(_status, "success", False)


def unavailable_reason():
    if available():
        return ""
    return getattr(_status, "reason", "") or configuration_error() or "OpenRouter has not been called in this thread"


def without_scores(docs, top_k=None):
    """Do not mutate retrieved objects or retain scores from a previous attempt."""
    result = []
    for doc in docs:
        clone = copy.copy(doc)
        clone.metadata = dict(getattr(doc, "metadata", None) or {})
        for name in ("rerank_score", "rerank_aspect", "rerank_backend", "rerank_model"):
            clone.metadata.pop(name, None)
        result.append(clone)
    return result[:top_k] if top_k else result


def _http_client():
    global _client
    with _client_lock:
        if _client is None:
            # HTTPX's synchronous Client can be shared across threads. Construct
            # it after worker startup, not while importing modules before a fork.
            import httpx

            _client = httpx.Client(
                follow_redirects=False,
                limits=httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY),
            )
        return _client


def _back_off(response):
    global _cooldown_until
    default = 60 if response.status_code in (401, 402, 403, 429) else 5
    delay = default
    header = response.headers.get("Retry-After", "")
    try:
        delay = float(header)
    except (ValueError, TypeError):
        try:
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delay = (when - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            pass
    if not math.isfinite(delay):
        delay = default
    with _cooldown_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + max(1, min(delay, 86400)))


def _fetch_scores(query, documents, remaining):
    timeout = remaining()
    with _cooldown_lock:
        if time.monotonic() < _cooldown_until:
            raise RerankFailure("OpenRouter cooldown is active")
    payload = {"model": MODEL_NAME, "query": query, "documents": documents, "top_n": len(documents)}
    with _http_client().stream(
        "POST", ENDPOINT, json=payload,
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY'].strip()}",
                 "Accept": "application/json", "Accept-Encoding": "identity"},
        timeout=timeout,
    ) as response:
        if response.status_code != 200:
            if response.status_code in (401, 402, 403, 429) or response.status_code >= 500:
                _back_off(response)
            raise RerankFailure(f"OpenRouter HTTP {response.status_code}")
        raw = bytearray()
        for chunk in response.iter_bytes():
            remaining()
            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                raise RerankFailure("OpenRouter response exceeded its size limit")
            raw.extend(chunk)
        remaining()
        data = json.loads(raw)

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or len(results) != len(documents):
        raise RerankFailure("OpenRouter returned an incomplete ranking")
    scores = [None] * len(documents)
    for row in results:
        if not isinstance(row, dict):
            raise RerankFailure("OpenRouter returned an invalid ranking")
        index, score = row.get("index"), row.get("relevance_score")
        if (type(index) is not int or not 0 <= index < len(documents)
                or scores[index] is not None or type(score) not in (int, float)
                or not math.isfinite(float(score))):
            raise RerankFailure("OpenRouter returned an invalid index or score")
        scores[index] = float(score)
    return scores


def rerank_queries(queries, docs, *, top_k=None, max_pairs=40, diversify=False, request_budget=None):
    """Score bounded query/doc batches and preserve retrieval order on failure.

    Ordinary variants use each document's best score. Multi-part questions merge
    per-aspect rankings round-robin. All selected aspects must succeed; a partial
    API failure never silently starves one part or triggers a local/paid fallback.
    """
    _status.success, _status.reason = False, ""
    original = without_scores(list(docs))

    def fallback(reason):
        _status.reason = reason
        logger.info("OpenRouter reranker skipped: %s", reason)
        return original[:top_k] if top_k else original

    error = configuration_error()
    if error:
        return fallback(error)
    clean = []
    for query in queries or []:
        if isinstance(query, str):
            query = query.strip().encode("utf-8")[:2048].decode("utf-8", errors="ignore")
            if query and query not in clean:
                clean.append(query)
    clean = clean[:MAX_QUERIES]
    owners, texts = [], []
    for index, doc in enumerate(original[:max(0, min(max_pairs, MAX_DOCUMENTS))]):
        metadata = doc.metadata
        header = " ".join(str(metadata[k]) for k in ("section", "header") if metadata.get(k))
        text = (header + "\n" + (getattr(doc, "page_content", "") or "")).strip()
        text = text[:MAX_DOCUMENT_CHARS].encode("utf-8")[:8192].decode("utf-8", errors="ignore")
        if text:
            owners.append(index)
            texts.append(text)
    if not clean or not texts:
        return fallback("no query or document text")
    if not _slots.acquire(blocking=False):
        return fallback("OpenRouter concurrency limit reached")
    deadline = time.monotonic() + TIMEOUT_SECONDS

    def remaining():
        seconds = deadline - time.monotonic()
        if request_budget is not None:
            seconds = min(seconds, request_budget.remaining())
        if seconds <= 0:
            raise TimeoutError("Reranking time budget exhausted")
        return seconds

    try:
        matrix = [_fetch_scores(query, texts, remaining) for query in clean]
        remaining()
    except RerankFailure as failure:
        return fallback(str(failure))
    except Exception as failure:  # noqa: BLE001 - optional scoring must not fail chat
        # Never log exception bodies: HTTP clients may include credentials or data.
        return fallback(f"OpenRouter request failed ({type(failure).__name__})")
    finally:
        _slots.release()

    best = [max(row[i] for row in matrix) for i in range(len(texts))]
    rankings = [sorted(range(len(texts)), key=lambda i: row[i], reverse=True) for row in matrix]
    keys = [
        ("id", str(doc.metadata["chunk_id"])) if doc.metadata.get("chunk_id") else
        ("text", str(doc.metadata.get("source", "")), str(doc.metadata.get("page", "")),
         getattr(doc, "page_content", ""))
        for doc in original
    ]
    selected, seen = [], set()
    if diversify:
        cursors = [0] * len(rankings)
        while True:
            progressed = False
            for aspect, ranking in enumerate(rankings):
                while cursors[aspect] < len(ranking):
                    candidate = ranking[cursors[aspect]]
                    cursors[aspect] += 1
                    key = keys[owners[candidate]]
                    if key in seen:
                        continue
                    selected.append((candidate, clean[aspect]))
                    seen.add(key)
                    progressed = True
                    break
            if not progressed:
                break
    else:
        selected = [(i, None) for i in sorted(range(len(texts)), key=lambda i: best[i], reverse=True)]

    ranked = []
    for candidate, aspect in selected:
        doc = original[owners[candidate]]
        doc.metadata.update(rerank_score=best[candidate], rerank_backend="openrouter", rerank_model=MODEL_NAME)
        if aspect is not None:
            doc.metadata["rerank_aspect"] = aspect
        ranked.append(doc)
    for i, doc in enumerate(original):
        if i not in owners and (not diversify or keys[i] not in seen):
            ranked.append(doc)
            seen.add(keys[i])
    _status.success, _status.reason = True, ""
    return ranked[:top_k] if top_k else ranked
