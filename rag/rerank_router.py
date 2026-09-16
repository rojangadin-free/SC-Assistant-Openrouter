"""
rag/rerank_router.py — one call site, two backends.

`rag/chain.py` used to `from rag.reranker import rerank, rerank_multi`, which
binds the LOCAL implementation at import time. That is the one thing a runtime
toggle cannot survive: an admin flipping the switch would change the stored
setting and change nothing about the running app, because the function object in
`chain`'s namespace was chosen when the module was first imported.

So the chain now imports from here, and these two functions read the setting on
every call and dispatch. The indirection is a dictionary lookup and a string
compare next to a network round-trip or a cross-encoder batch — unmeasurable —
and it is what makes the switch actually mean something.

Fallback between backends
-------------------------
When the chosen backend cannot rank (no key on `api`, no weights on `local`), the
OTHER one is tried before giving up. This is deliberate and it is the behaviour
that matters most on a fresh machine:

    api chosen, key missing        -> local, if it has weights
    local chosen, weights missing  -> api, if a key exists
    neither works                  -> retrieval order, exactly as before

Without it, a mis-set toggle silently costs every student their answer ordering
while both `available()` accessors keep reporting the truth to a log nobody
reads. The switch expresses a PREFERENCE, not a suicide pact.

`last_used()` records which one actually ran, so the retrieval log in
`rag/chain.py` can name the backend rather than printing `score=n/a` and leaving
the reader to guess whether the toggle took effect.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional, Sequence

from rag import rerank_settings

logger = logging.getLogger(__name__)

# Which backend produced the ordering of the most recent request, per thread.
#
# Thread-local rather than a module global: gunicorn runs `--threads 4`, so two
# students can be inside this module at once, and a shared global would let one
# request's log line name the other request's backend. A wrong attribution in a
# diagnostic is worse than no attribution.
_state = threading.local()


def _remember(name: str) -> None:
    _state.last = name


def last_used() -> str:
    """
    The backend that ranked the current thread's most recent request:
    "local", "api", or "" when nothing ranked it (retrieval order was kept).
    """
    return getattr(_state, "last", "")


def _backends():
    """
    The two implementations, imported lazily.

    Lazy for a real reason: importing `rag.reranker` at module scope would mean
    every consumer of this router pays for the local reranker's module-level
    thread pinning and env setup even when the API backend is the one selected.
    """
    from rag import api_reranker
    from rag import reranker

    return {rerank_settings.LOCAL: reranker, rerank_settings.API: api_reranker}


def _order(preferred: str) -> List[str]:
    """The chosen backend first, the other as the fallback."""
    other = rerank_settings.API if preferred == rerank_settings.LOCAL else rerank_settings.LOCAL
    return [preferred, other]


def _usable(mod, name: str) -> bool:
    """
    Can this backend rank right now?

    `local` is usable when it is not switched off AND either already loaded or
    still able to try (a load that has failed once is not retried, and
    `unavailable_reason()` reports why). `api` is usable when a key exists.

    Deliberately cheap and deliberately optimistic: this must not trigger a model
    load or a probe request, so "usable" means "worth attempting", and the
    attempt's own fallback handles being wrong.
    """
    try:
        if name == rerank_settings.LOCAL:
            if getattr(mod, "disabled", lambda: False)():
                return False
            if getattr(mod, "available", lambda: False)():
                return True
            # Not loaded yet is not a failure — the first call loads it. A load
            # that already failed sets _load_failed, and retrying it every
            # question would pay the failure repeatedly.
            return not getattr(mod, "_load_failed", False)
        return getattr(mod, "available", lambda: False)()
    except Exception:
        return False


def active_backend() -> str:
    """
    The backend a question asked right now would actually use.

    Not the same thing as `rerank_settings.current()`: that is what an admin
    CHOSE, this accounts for whether the choice is presently achievable. The
    admin screen shows both, because "api (falling back to local)" is the state
    someone needs to see, and it is invisible if you only report the setting.
    """
    chosen = rerank_settings.current()
    mods = _backends()
    for name in _order(chosen):
        if _usable(mods[name], name):
            return name
    return ""


def rerank(
    query: str,
    docs: Sequence,
    top_k: Optional[int] = None,
    max_pairs: Optional[int] = None,
    queries: Optional[Sequence[str]] = None,
) -> List:
    """
    Rank `docs` with whichever backend is live. Same contract as both
    implementations: returns documents in some order, never raises.

    `max_pairs` defaults to None rather than to a number, so each backend applies
    its OWN candidate cap. Hardcoding one value here would silently impose the
    local reranker's window budget on the API backend, which pays for candidates
    by request size rather than by CPU.
    """
    if not docs:
        return []

    chosen = rerank_settings.current()
    mods = _backends()

    for name in _order(chosen):
        mod = mods[name]
        if not _usable(mod, name):
            continue

        kwargs = {"top_k": top_k, "queries": queries}
        if max_pairs is not None:
            kwargs["max_pairs"] = max_pairs

        try:
            out = mod.rerank(query, docs, **kwargs)
        except Exception as e:
            # Both backends already swallow their own failures, so reaching here
            # means something unexpected. Try the other one rather than losing
            # the ordering entirely.
            logger.warning("Reranker %s raised, trying the other backend: %s", name, e)
            print(f"  Reranker '{name}' raised (non-fatal): {e}")
            continue

        # Did it actually score anything? A backend that fell back internally
        # returns the input order with no scores, and reporting that as "ranked
        # by api" would be a lie in the one place someone looks for the truth.
        if _scored(out):
            if name != chosen:
                logger.info("Reranker fell back from %s to %s", chosen, name)
                print(f"  Reranker: '{chosen}' unavailable, used '{name}' instead")
            _remember(name)
            return out

    _remember("")
    return list(docs)[:top_k] if top_k else list(docs)


def rerank_multi(
    aspects: Sequence[str],
    docs: Sequence,
    top_k: Optional[int] = None,
    max_pairs: Optional[int] = None,
) -> List:
    """
    Multi-aspect ranking with whichever backend is live.

    Both implementations guarantee the same property — round-robin across the
    parts of a multi-part question, so no ask is starved of slots — so the caller
    does not need to know which one ran.
    """
    if not docs:
        return []

    chosen = rerank_settings.current()
    mods = _backends()

    for name in _order(chosen):
        mod = mods[name]
        if not _usable(mod, name):
            continue

        kwargs = {"top_k": top_k}
        if max_pairs is not None:
            kwargs["max_pairs"] = max_pairs

        try:
            out = mod.rerank_multi(aspects, docs, **kwargs)
        except Exception as e:
            logger.warning("Reranker %s raised, trying the other backend: %s", name, e)
            print(f"  Reranker '{name}' raised (non-fatal): {e}")
            continue

        if _scored(out):
            if name != chosen:
                logger.info("Reranker fell back from %s to %s", chosen, name)
                print(f"  Reranker: '{chosen}' unavailable, used '{name}' instead")
            _remember(name)
            return out

    _remember("")
    return list(docs)[:top_k] if top_k else list(docs)


def _scored(docs) -> bool:
    """
    True when at least one document carries a `rerank_score`.

    This is the only honest test of "did reranking happen": both backends return
    the documents untouched when they cannot rank, so the return value alone
    cannot be distinguished from a successful rank. The score is the evidence.
    """
    for d in docs or []:
        md = getattr(d, "metadata", None) or {}
        if "rerank_score" in md:
            return True
    return False


def available() -> bool:
    """True when a question asked now would come back with scores."""
    return bool(active_backend())


def unavailable_reason() -> str:
    """
    Why no backend can rank, or "" when one can.

    Both reasons are reported, not just the chosen one's: an operator who set
    `api` and has no key needs to know that local ALSO has no weights, otherwise
    they fix one problem, retry, and see the identical symptom.
    """
    if available():
        return ""

    chosen = rerank_settings.current()
    mods = _backends()
    parts = []
    for name in _order(chosen):
        try:
            reason = mods[name].unavailable_reason() or "unavailable"
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        parts.append(f"{name}: {reason}")
    return "; ".join(parts)


def describe() -> dict:
    """The setting, the effective backend, and both readiness reports."""
    info = rerank_settings.describe()
    info["active"] = active_backend()
    info["falling_back"] = bool(info["active"]) and info["active"] != info["backend"]
    return info
