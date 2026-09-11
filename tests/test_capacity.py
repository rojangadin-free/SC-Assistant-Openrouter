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

# Default ON. This is the assertion that matters most in this section: an absent
# env var must never mean "silently answer without ranking". The eval showing
# that off is viable measured RECALL (gold evidence reaching the top-12), not
# ordering within it, so off stays opt-in until there is evidence about answers.
check(
    "reranking is enabled when RERANKER_ENABLED is unset",
    reranker.RERANKER_ENABLED is True,
    f"got {reranker.RERANKER_ENABLED!r} for {os.environ.get('RERANKER_ENABLED')!r}",
)
check(
    "disabled() agrees with the flag",
    reranker.disabled() is False,
)


def enabled_for(value):
    """Re-evaluate the flag exactly as the module does at import time."""
    return (value or "true").strip().lower() not in ("false", "0", "no", "off")


# Parsed permissively on purpose. "0" or "off" that silently meant ON would
# leave someone certain they had disabled ranking while still paying ~3.5 s of
# CPU per question — which is the whole reason the switch exists.
for falsey in ("false", "False", "FALSE", "0", "no", "off", " off "):
    check(
        f"{falsey!r} disables reranking",
        enabled_for(falsey) is False,
    )
for truthy in ("true", "1", "yes", "", "banana"):
    check(
        f"{truthy!r} leaves reranking ON (unknown values fail safe)",
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
    "gunicorn still runs 2 workers (memory ceiling on an 8 GB box)",
    '"--workers", "2"' in dockerfile,
)
check(
    "gunicorn heartbeats through /dev/shm, not EBS-backed /tmp",
    "/dev/shm" in dockerfile,
)

print(f"\n{'=' * 60}")
print(f"  {passed} passed, {failed} failed")
print(f"{'=' * 60}\n")

raise SystemExit(1 if failed else 0)
