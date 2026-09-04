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

Agreeing on an id is necessary but not sufficient: the pinned stack must also be
able to LOAD it. Pointing both files at `cross-encoder/ettin-reranker-32m-v1`
while `requirements.txt` still pinned `sentence-transformers==3.3.1` turned the
silent degradation into a red build —

    ValueError: Tokenizer class TokenizersBackend does not exist
                or is not currently imported.

— because 3.3.1 holds `transformers` on the 4.x line, and that model's
tokenizer_config names a class only the newer line ships (its config.json is also
`model_type: modernbert`, another 5.x concept). Same dev-only illusion as before:
newer transformers plus a warm cache on one laptop, broken everywhere else.

The pins have since been raised to `sentence-transformers==5.7.0` /
`transformers==5.16.1` and the ettin model is now the configured default, so the
pairing check below is what stops a future "let's roll sentence-transformers back
to something older" from silently disabling reranking again. It is also why
`transformers` is pinned explicitly rather than left to pip: the reranker's
loadability depends on that line, so it is a declared requirement, not a
transitive accident.


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

# The model must be loadable by the versions the container actually installs.
requirements_src = read("requirements.txt")
st_pin = re.search(r"^sentence-transformers==(\d+)\.", requirements_src, re.M)
st_major = int(st_pin.group(1)) if st_pin else 0

tf_pin = re.search(r"^transformers==(\d+)\.", requirements_src, re.M)
tf_major = int(tf_pin.group(1)) if tf_pin else 0

check("requirements.txt pins sentence-transformers", st_major > 0)
check(
    "the configured reranker's tokenizer exists in the pinned transformers line",
    st_major >= 5 or "ettin" not in app_model.lower(),
    f"sentence-transformers {st_major}.x cannot construct {app_model}",
)
# transformers is what actually constructs the tokenizer, so it is pinned in its
# own right instead of being inherited from whatever sentence-transformers happens
# to allow. Left implicit, a rebuild could pick up a different major line and
# reintroduce the TokenizersBackend failure with no diff to point at.
check(
    "requirements.txt pins transformers explicitly",
    tf_major > 0,
    "the reranker's loadability depends on this line; it must not be transitive",
)
check(
    "the pinned transformers line can construct the configured reranker",
    tf_major >= 5 or "ettin" not in app_model.lower(),
    f"transformers {tf_major}.x cannot construct {app_model}",
)

check(
    "max_length is a named constant, not a figure borrowed from another model",
    "max_length=RERANKER_MAX_LENGTH" in reranker_src
    and "max_length=8192" not in reranker_src,
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
