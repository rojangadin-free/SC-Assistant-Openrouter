"""
The reranker model id must be the SAME in the app and in the downloader.

Why this suite exists
---------------------
It wasn't, and the bug it guards against is invisible. `download_model.py`
pre-downloaded `cross-encoder/ms-marco-MiniLM-L6-v2` while `rag/reranker.py`
loaded `cross-encoder/ettin-reranker-32m-v1`, so the model baked into the Docker
image was never the model the app asked for.

Nothing raised. `_load_model()` catches the failure by design, `rerank()` returns
the candidates untouched, no document gets a `rerank_score`, and the retrieval
log prints `score=n/a` for all 12 documents while answers silently fall back to
raw hybrid-retrieval order. It only reproduced on machines OTHER than the
developer's, because a local HuggingFace cache had the right model from an
earlier interactive run — the worst possible failure shape: correct here, quietly
degraded everywhere else.

These are source-text assertions on purpose. The ids must match without
downloading ~90 MB of weights or importing sentence_transformers, so the suite
stays runnable in CI and on a laptop with no network.
"""

import os
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


def literal(src, name):
    """The string assigned to `name = "..."` at module level."""
    m = re.search(rf'^{name}\s*=\s*["\']([^"\']+)["\']', src, re.M)
    return m.group(1) if m else ""


print("\nReranker model wiring")
print("-" * 60)

reranker_src = read("rag/reranker.py")
download_src = read("download_model.py")
startup_src = read("sc_assistant/__init__.py")
chain_src = read("rag/chain.py")

app_model = literal(reranker_src, "DEFAULT_RERANKER_MODEL")
dl_model = literal(download_src, "DEFAULT_RERANKER_MODEL")

check("rag/reranker.py declares DEFAULT_RERANKER_MODEL", bool(app_model))
check("download_model.py declares DEFAULT_RERANKER_MODEL", bool(dl_model))
check(
    "the app and the downloader agree on the reranker id",
    bool(app_model) and app_model == dl_model,
    f"app={app_model!r} downloader={dl_model!r}",
)

# The original defect: os.getenv's FIRST argument is the variable name, so
# os.getenv("cross-encoder/...", "cross-encoder/...") looked up an env var named
# after the model and returned the default by accident. RERANKER_MODEL_NAME was
# dead — a deployment could set it and nothing changed.
check(
    "the app reads the model from the RERANKER_MODEL_NAME env var",
    'os.getenv("RERANKER_MODEL_NAME"' in reranker_src,
)
check(
    "no env var is named after a model id",
    'os.getenv("cross-encoder/' not in reranker_src,
)
check(
    "the downloader honours the same env var",
    'os.getenv("RERANKER_MODEL_NAME"' in download_src,
)

# A silent degradation has to announce itself somewhere a human is looking.
check(
    "startup acts on warmup()'s result instead of discarding it",
    "if warmup():" in startup_src,
)
check(
    "the startup warning names the fix",
    "download_model.py" in startup_src,
)
check(
    "the retrieval log distinguishes 'not reranked' from 'reranked'",
    "NOT reranked" in chain_src,
)

# Live check: importing the module must stay free (no sentence_transformers, no
# network), and the state accessors must be honest before any load is attempted.
import rag.reranker as rr  # noqa: E402

check("reranker exposes available()", callable(getattr(rr, "available", None)))
check(
    "reranker exposes unavailable_reason()",
    callable(getattr(rr, "unavailable_reason", None)),
)
check("available() is False before any load", rr.available() is False)
check(
    "unavailable_reason() explains an unattempted load",
    rr.unavailable_reason() == "not loaded yet",
    repr(rr.unavailable_reason()),
)
check(
    "importing rag.reranker does not pull sentence_transformers",
    "sentence_transformers" not in sys.modules,
)

# The env var override must actually reach the module.
os.environ["RERANKER_MODEL_NAME"] = "cross-encoder/probe-model"
try:
    import importlib

    reloaded = importlib.reload(rr)
    check(
        "setting RERANKER_MODEL_NAME changes the model the app loads",
        reloaded.RERANKER_MODEL_NAME == "cross-encoder/probe-model",
        reloaded.RERANKER_MODEL_NAME,
    )
finally:
    del os.environ["RERANKER_MODEL_NAME"]
    importlib.reload(rr)

print("-" * 60)
if failures:
    print(f"  {len(failures)} check(s) failed\n")
    sys.exit(1)
print("  all checks passed\n")
