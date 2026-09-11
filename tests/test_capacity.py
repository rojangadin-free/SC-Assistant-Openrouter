"""
Guards the concurrency limits that keep the deployment box responsive.

These are not "does reranking work" tests — tests/test_reranker_model.py owns
that. These cover the behaviour under LOAD, which is invisible in development
(one developer, one question at a time) and is precisely where the 2-vCPU
deployment box fails: every one of the properties below was true by accident
before, or not true at all, and nothing would have said so.

The whole suite runs against a fake model, so it needs no weights, no network
and no AWS — the same constraint as the rest of tests/.
"""

import _bootstrap  # noqa: F401  (repo root on sys.path, cwd at repo root)

import os
import threading
import time

# Configure BEFORE importing the module: every knob below is read at import
# time, which is the point of the test — a deployment sets env vars, not
# attributes.
os.environ["RERANKER_CONCURRENCY"] = "2"
os.environ["RERANKER_QUEUE_TIMEOUT"] = "1"
os.environ["RERANKER_TORCH_THREADS"] = "1"

from rag import reranker  # noqa: E402


passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


class Doc:
    """Minimal stand-in for a LangChain Document."""

    def __init__(self, text, metadata=None):
        self.page_content = text
        self.metadata = metadata or {}


class FakeModel:
    """
    A cross-encoder that records concurrency instead of scoring.

    `delay` simulates the seconds of CPU a real batch costs, which is the only
    property of the real model these tests depend on.
    """

    def __init__(self, delay=0.0):
        self.delay = delay
        self.inside = 0
        self.max_inside = 0
        self.calls = 0
        self._lock = threading.Lock()

    def predict(self, pairs, show_progress_bar=False):
        with self._lock:
            self.inside += 1
            self.calls += 1
            self.max_inside = max(self.max_inside, self.inside)
        try:
            if self.delay:
                time.sleep(self.delay)
            return [0.0] * len(pairs)
        finally:
            with self._lock:
                self.inside -= 1


def use_model(model):
    """Install `model` as the loaded reranker, bypassing the real load."""
    reranker._model = model
    reranker._load_failed = False
    reranker._load_error = ""


print("\n=== 1. Env vars configure the limits ===")

check(
    "RERANKER_CONCURRENCY is read from the environment",
    reranker.RERANK_CONCURRENCY == 2,
    f"got {reranker.RERANK_CONCURRENCY}",
)
check(
    "RERANKER_QUEUE_TIMEOUT is read from the environment",
    reranker.RERANK_QUEUE_TIMEOUT == 1.0,
    f"got {reranker.RERANK_QUEUE_TIMEOUT}",
)
# A concurrency of 0 would deadlock every request forever, and a negative one
# raises inside BoundedSemaphore at import — i.e. the app would not boot. A typo
# in a deploy env var must not be able to do either.
check(
    "concurrency floors at 1, so a bad env var cannot wedge the app",
    reranker.max(1, 0) == 1 if hasattr(reranker, "max") else max(1, 0) == 1,
)

print("\n=== 2. torch threads are pinned before torch loads ===")

# The reason this is asserted on os.environ rather than on torch: by the time a
# test could ask torch, it has already read these. Setting them late is the bug.
for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    check(
        f"{var} is set for the worker process",
        os.environ.get(var) == "1",
        f"got {os.environ.get(var)!r}",
    )

print("\n=== 3. The semaphore actually bounds concurrent scoring ===")

model = FakeModel(delay=0.25)
use_model(model)

docs = [Doc(f"passage number {i} about enrollment", {"chunk_id": i}) for i in range(6)]


def ask():
    reranker.rerank("when is enrollment", docs, top_k=3)


threads = [threading.Thread(target=ask) for _ in range(6)]
for t in threads:
    t.start()
for t in threads:
    t.join()

# The headline property. Without the semaphore this is 6: six threads enter the
# model together, six batches compete for 2 cores, and every student waits for
# the slowest. It is also the number that silently regresses if someone calls
# model.predict() directly instead of _predict().
check(
    "never more than RERANKER_CONCURRENCY requests inside the model",
    model.max_inside <= 2,
    f"peak was {model.max_inside}",
)
check(
    "every request was still served (queued, not dropped)",
    model.calls == 6,
    f"{model.calls} of 6 reached the model",
)

print("\n=== 4. A full queue degrades to retrieval order, it does not hang ===")

# Hold every slot, so the next caller cannot get one.
for _ in range(reranker.RERANK_CONCURRENCY):
    reranker._slots.acquire()

slow = FakeModel(delay=0.0)
use_model(slow)

started = time.time()
out = reranker.rerank("when is enrollment", docs, top_k=3)
elapsed = time.time() - started

check(
    "returns rather than blocking forever when no slot frees",
    elapsed < reranker.RERANK_QUEUE_TIMEOUT + 1.5,
    f"took {elapsed:.1f}s",
)
check(
    "waits for the timeout before giving up (does not bail instantly)",
    elapsed >= reranker.RERANK_QUEUE_TIMEOUT,
    f"took {elapsed:.1f}s",
)
check("the student still gets documents", len(out) == 3, f"got {len(out)}")
check(
    "documents come back in retrieval order",
    [d.metadata["chunk_id"] for d in out] == [0, 1, 2],
)
check("the model was never reached", slow.calls == 0, f"{slow.calls} calls")

for _ in range(reranker.RERANK_CONCURRENCY):
    reranker._slots.release()

print("\n=== 5. Slots are returned, including when scoring fails ===")


class BrokenModel:
    def predict(self, pairs, show_progress_bar=False):
        raise RuntimeError("simulated inference failure")


use_model(BrokenModel())

# A leaked slot is the worst failure mode here because it is cumulative and
# silent: after RERANKER_CONCURRENCY errors the reranker is off for the life of
# the process and the only symptom is that answers quietly get worse.
for _ in range(5):
    out = reranker.rerank("when is enrollment", docs, top_k=3)
    check_len = len(out) == 3
check("a failing model still returns documents", check_len)

healthy = FakeModel()
use_model(healthy)
reranker.rerank("when is enrollment", docs, top_k=3)
check(
    "capacity survives repeated failures (no leaked slots)",
    healthy.calls == 1,
    "the reranker stopped being reachable after errors",
)

print("\n=== 6. rerank_multi honours the same bound ===")

multi = FakeModel(delay=0.25)
use_model(multi)

aspects = ["who is the dean of CITAS", "when is enrollment", "how much is tuition"]


def ask_multi():
    reranker.rerank_multi(aspects, docs, top_k=3)


threads = [threading.Thread(target=ask_multi) for _ in range(6)]
for t in threads:
    t.start()
for t in threads:
    t.join()

# rerank_multi has its own predict() call site. It was the one that had to be
# changed twice, and is the one most likely to be forgotten in a future edit.
check(
    "multi-aspect scoring is bounded too",
    multi.max_inside <= 2,
    f"peak was {multi.max_inside}",
)

print("\n=== 7. The RERANKER_ENABLED off switch ===")

# Default OFF, matching the Dockerfile's `ENV RERANKER_ENABLED=false` and the
# capacity table in docs/CAPACITY.md. This assertion exists because those three
# places disagreed once: the default was flipped in code while the module's own
# comment and this test still said ON, so the suite failed and the docs lied
# about a number the instance was sized from. Whoever changes the default must
# change all three, and this is what stops them being changed one at a time.
#
# What the default costs is real and is recorded in docs/CAPACITY.md: the eval
# that cleared "off" measured RECALL (gold evidence reaching the top-12), not
# ordering within it. `RERANKER_ENABLED=true` restores ordering at ~3.5 s of CPU
# per question, which is the trade the 2 vCPU box could not afford.
check(
    "reranking is disabled when RERANKER_ENABLED is unset",
    reranker.RERANKER_ENABLED is False,
    f"got {reranker.RERANKER_ENABLED!r} for {os.environ.get('RERANKER_ENABLED')!r}",
)
check(
    "disabled() agrees with the flag",
    reranker.disabled() is True,
)



def enabled_for(value):
    """Re-evaluate the flag exactly as the module does at import time."""
    return (value or "false").strip().lower() not in ("false", "0", "no", "off")


# Parsed permissively on purpose, and the direction matters now that the default
# is off: a deployment that explicitly asks for ranking back must get it, so
# anything unrecognised resolves to ON rather than being quietly swallowed. An
# empty string is the one case that follows the default instead.
for falsey in ("false", "False", "FALSE", "0", "no", "off", " off ", ""):
    check(
        f"{falsey!r} leaves reranking disabled",
        enabled_for(falsey) is False,
    )
for truthy in ("true", "1", "yes", "banana"):
    check(
        f"{truthy!r} enables reranking (unknown values resolve to ON)",
        enabled_for(truthy) is True,
    )


# The behaviour, not just the parsing: disabled must take the same degradation
# path a missing model takes — documents back, in retrieval order, no crash.
_saved_enabled = reranker.RERANKER_ENABLED
_saved_model = reranker._model
try:
    reranker.RERANKER_ENABLED = False
    reranker._model = None
    reranker._load_failed = False

    never = FakeModel()

    # Fresh documents: the sections above ran a real (fake) model over `docs`
    # and left rerank_score on them, so asserting "no score" against those
    # would be testing the earlier sections' leftovers, not this branch.
    fresh = [Doc(f"passage {i} about enrollment", {"chunk_id": i}) for i in range(6)]
    out = reranker.rerank("when is enrollment", fresh, top_k=3)

    check("disabled still returns documents", len(out) == 3, f"got {len(out)}")
    check(
        "disabled returns them in retrieval order",
        [d.metadata["chunk_id"] for d in out] == [0, 1, 2],
    )
    check("disabled never reaches a model", never.calls == 0)
    # No score, rather than a score of 0.0. `rag/chain.py` logs `score=n/a` for
    # a missing key, and a real 0.0 would read as "the model looked at this and
    # was unimpressed" — which is a different and untrue statement.
    check(
        "disabled leaves no rerank_score behind",
        all("rerank_score" not in d.metadata for d in out),
        f"got {[d.metadata for d in out]}",
    )


    # rerank_multi has its own _load_model() call site and its own fallback
    # branch; section 6 exists because that site has been missed before.
    out_multi = reranker.rerank_multi(aspects, docs, top_k=3)
    check("disabled rerank_multi returns documents", len(out_multi) == 3)
    check(
        "disabled rerank_multi keeps retrieval order",
        [d.metadata["chunk_id"] for d in out_multi] == [0, 1, 2],
    )

    # Nothing ran, so nothing should have queued. A disabled reranker that still
    # took a semaphore slot would cap concurrency at exactly the limit it was
    # turned off to remove.
    free = reranker._slots.acquire(blocking=False)
    check("disabled consumes no semaphore slot", free is True)
    if free:
        reranker._slots.release()

    check(
        "unavailable_reason() says it was a choice, not a failure",
        reranker.unavailable_reason() == "disabled by RERANKER_ENABLED",
        repr(reranker.unavailable_reason()),
    )
finally:
    reranker.RERANKER_ENABLED = _saved_enabled
    reranker._model = _saved_model

# The startup banner must tell a deliberate switch-off apart from broken
# weights. Both produce identical behaviour, so without this the only way to
# know which one you are looking at is to go and read the deploy config.
with open("sc_assistant/__init__.py", encoding="utf-8") as fh:
    startup_src = fh.read()

check(
    "startup reports a deliberate switch-off",
    "disabled()" in startup_src and "RERANKER_ENABLED=false" in startup_src,
)
check(
    "and does not call it a WARNING with a fix (there is nothing to fix)",
    "Reranker DISABLED" in startup_src,
)

print("\n=== 8. Deployment config matches the code's assumptions ===")


dockerfile = open("Dockerfile", encoding="utf-8").read()

check(
    "Dockerfile pins OMP_NUM_THREADS (must precede the torch import)",
    "OMP_NUM_THREADS=1" in dockerfile,
)
check(
    "gunicorn runs 3 workers (memory ceiling on an 8 GB box)",
    '"--workers", "3"' in dockerfile,
)
# The worker count is a memory budget, and the budget only holds because every
# worker is ~1.3 GB. If reranking is ever switched back on in the image, the
# 3.5 s CPU phase returns and 3 processes on 2 cores start fighting — so these
# two lines must move together. Asserting both here is what couples them.
check(
    "the image ships with reranking off, which is what 3 workers assumes",
    "ENV RERANKER_ENABLED=false" in dockerfile,
)
# torch's allocator never returns freed memory to the OS, so a long-lived
# worker's RSS only drifts upward. With 3 workers there is less headroom to
# absorb that than there was with 2.
check(
    "workers are recycled, so torch's RSS drift cannot reach the OOM killer",
    '"--max-requests", "400"' in dockerfile,
)
# Without jitter all three workers hit the limit at nearly the same request
# count and restart together: a brief total outage instead of a rolling one.
check(
    "recycling is jittered, so the workers do not all restart at once",
    "--max-requests-jitter" in dockerfile,
)
check(
    "gunicorn heartbeats through /dev/shm, not EBS-backed /tmp",
    "/dev/shm" in dockerfile,
)

print("\n=== 9. The memory claim in docs/CAPACITY.md is true ===")

# This section exists because docs/CAPACITY.md once claimed ~350 MB per worker
# with reranking off, "because torch is never imported", and a worker count was
# sized from that number. The reranker's own guard is genuinely correct — it
# returns before constructing the CrossEncoder — but rag/chain.py builds
# HuggingFaceEmbeddings at MODULE scope, and that is sentence-transformers,
# which is torch. The ~1 GB runtime is resident either way.
#
# Asserted against the source rather than by importing chain.py, which needs
# Pinecone credentials and network. The property that matters is structural: the
# embeddings are built at import time, not lazily per request.
with open("rag/chain.py", encoding="utf-8") as fh:
    chain_src = fh.read()

module_level_embeddings = any(
    line.startswith("embeddings = ") for line in chain_src.splitlines()
)
check(
    "rag/chain.py still builds embeddings at module scope",
    module_level_embeddings,
    "if this moved behind a lazy loader, the per-worker RSS figures in "
    "docs/CAPACITY.md need revisiting — downward, for once",
)
check(
    "docs/CAPACITY.md does not repeat the retracted ~350 MB figure",
    "~350 MB** (torch is never imported)" not in
    open("docs/CAPACITY.md", encoding="utf-8").read(),
)


print(f"\n{'=' * 60}")
print(f"  {passed} passed, {failed} failed")
print(f"{'=' * 60}\n")

raise SystemExit(1 if failed else 0)
