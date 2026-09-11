# Capacity

How many students can use this at once, on the box it actually runs on.

The deployment target is a single **`m7i-flex.large`** — 2 vCPU, 8 GB RAM —
running the container from the `Dockerfile` at the repo root. Everything below is
derived from that shape and from the code in `rag/chain.py` and
`rag/reranker.py`, not from a general rule of thumb about Flask.

---

## The answer

| Question | Figure |
|---|---|
| Questions being *processed* at the same instant, without degradation | **2–4** |
| Students actively chatting (asking, then reading for 20–40 s) | **30–60** |
| Questions per hour | **~1,000–1,400** |
| Sessions merely open on the page | **hundreds** |
| Point where waits pass ~20 s and it feels broken | **~8–10 simultaneous questions** |

For a single campus assistant, 30–60 students mid-conversation is a busy day.
The number that should worry anyone is the last row, and the whole of the rest of
this document is about pushing it further away.

---

## Where a request's time goes

One question, traced through `rag/chain.py`:

| Phase | Wall clock | Resource |
|---|---|---|
| Optimizer LLM rewrite | 0 s, or ≤2.5 s | network |
| Pinecone hybrid fan-out (parallel probes) | ~0.5–1 s | network |
| **Cross-encoder rerank** | **~3–4 s** (up to ~16 s multi-part) | **CPU** |
| Prompt assembly, DynamoDB reads | ~0.2 s | network |
| LLM streaming, first token to last | ~2–8 s | network |
| **Total** | **~6–12 s** | |

The optimizer row is 0 s for a self-contained question because `rag/latency.py`
skips it — see the "Answer speed" section of the README.

Only **one** row of that table consumes the instance. Every other phase is a
socket wait, which is why the `Dockerfile` runs `--threads 4`: a thread parked on
a Pinecone read costs nothing, and without threads a few slow answers would
occupy both workers and queue everyone else at the front door.

So the shape of the problem is: **~6–12 s of latency per answer, of which ~3.5 s
is contended CPU.** 2 vCPU ÷ 3.5 s ≈ 0.55 questions/second in theory, ~0.3–0.4 in
practice. At one question per student per ~30 s of reading, that is the 30–60
figure above.

---

## Why memory, not CPU, fixes the worker count

Each gunicorn worker loads its own copy of every model:

| Component | Approx RSS |
|---|---|
| PyTorch + transformers runtime | ~700 MB–1 GB |
| MiniLM-L6 embeddings (384-d) | ~120 MB |
| `ettin-reranker-32m` cross-encoder | ~180 MB |
| Flask, LangChain, boto3, Pinecone client | ~250 MB |
| **Per worker** | **~1.3–1.6 GB** |

8 GB holds 2 workers comfortably, 3 tightly, 4 with a real chance of the OOM
killer taking a worker mid-answer. **More workers cannot buy more CPU here**, so
`--workers 2` is not a conservative guess — it is the ceiling, and it happens to
match the core count anyway.

---

## The three fixes applied

### 1. torch threads pinned to 1

`torch` starts one compute thread per core, per process. Two workers on two cores
therefore ask for **four** compute threads, and a measurable slice of every batch
goes to context switching rather than arithmetic. Worse, it is
*load-dependent* — invisible with one developer asking one question, and at its
worst exactly when the system is busiest.

Set in two places, deliberately:

- `Dockerfile` — `ENV OMP_NUM_THREADS=1` (plus MKL/OpenBLAS). These must be read
  **before** torch is imported, and an env var baked into the image is the only
  place guaranteed to precede that.
- `rag/reranker.py` — `os.environ.setdefault(...)` at module scope, and
  `torch.set_num_threads()` at load. Belt and braces for anyone running
  `python run.py` locally, without the Dockerfile's env.

This makes a single isolated rerank slightly *slower* and the box meaningfully
faster under concurrency. That is the correct trade for a shared tool.

### 2. A bounded queue in front of the model

`rag/reranker.py` now routes both `predict()` call sites through `_predict()`,
which holds one of `RERANKER_CONCURRENCY` (default **2**) slots.

Without it, load does not degrade gracefully — it degrades *uniformly*:

```
2 questions arrive -> 2 batches x ~3.5 s CPU on 2 cores -> both finish in ~4 s
8 questions arrive -> 8 batches fighting over 2 cores   -> ALL finish in ~14 s
```

The second case does no more work than the first. The only thing that changed is
that every student now waits for the slowest one. A semaphore turns that into a
queue: the first few run at full speed, the rest wait briefly for a slot. The
median student is strictly better off, and the waiting is already honestly
reported — `rag/progress.py` is showing "Reading the most relevant pages" while
this blocks, so the caption stays true.

**The wait is bounded.** After `RERANKER_QUEUE_TIMEOUT` (default 20 s) with no
slot, the request proceeds *without* reranking. A worse answer is a better
outcome than no answer, and it is the same graceful degradation the module
already had for a missing model. The release is in a `finally`, because a slot
leaked on an exception would permanently shrink capacity: after two failures the
reranker would be off for the life of the process, with nothing in the log
saying so.

### 3. `--worker-tmp-dir /dev/shm`

gunicorn heartbeats through a temp file. On an EBS-backed instance under load,
`/tmp` can stall long enough for the arbiter to decide a busy-but-healthy worker
has hung and kill it **mid-answer**. `/dev/shm` is memory, so the heartbeat
cannot be delayed by disk.

---

## The `flex` caveat

`m7i-flex` is not a fixed-performance instance. It is sized for workloads that
average **up to ~40% CPU**, bursting to 100% when needed. This app's reranker
pins both vCPUs at 100% for seconds at a time.

On a normal day that is fine: the average across an hour is low, and bursting is
exactly what the profile is for. During an enrollment-week rush, sustained demand
can run above the intended average and be pulled back toward baseline — at which
point **every figure in the first table roughly halves.**

Two ways out, in order of preference:

1. `m7i.large` — same 2 vCPU / 8 GB, fixed performance, no burst profile to
   exceed. The natural home for this workload.
2. `m7i.xlarge` — 4 vCPU / 16 GB. Roughly doubles the capacity table and allows
   `--workers 3`, which the memory budget above currently forbids.

---

## Turning the knobs

Everything is an env var, so a rush can be handled with `docker run -e` and no
rebuild.

| Variable | Default | Effect |
|---|---|---|
| `RERANKER_ENABLED` | `true` | `false` turns ranking off entirely. See below — this is the big one. |
| `RERANKER_CONCURRENCY` | `2` | Requests allowed inside the model at once. Raise on a bigger box. |
| `RERANKER_QUEUE_TIMEOUT` | `20` | Seconds to wait for a slot before answering from retrieval order. |
| `RERANKER_TORCH_THREADS` | `1` | Per-process compute threads. Raise only with fewer workers. |
| `RERANKER_MAX_TOTAL_WINDOWS` | `220` | **The biggest lever.** Total (query, window) pairs scored per request. |
| `RERANKER_MAX_PAIRS` | `40` | Candidates scored at all; the rest keep retrieval order. |

**Enrollment-week configuration**, roughly halving CPU per question for a small
ranking-quality cost:

```bash
docker run -e RERANKER_MAX_TOTAL_WINDOWS=120 \
           -e RERANKER_MAX_PAIRS=20 \
           ...
```

Worth knowing before reaching for that: `rerank_multi()` already divides the
window budget by the number of asks, so a three-part question does **not** cost
three times a single one (48 s → 16 s when that was fixed). The budget above is
per request, not per ask.

---

## Turning reranking off entirely

`RERANKER_ENABLED=false` removes the only CPU-bound phase in the table above.
Every figure in **The answer** changes, and it is the single largest lever in
this document:

| | Reranker ON | Reranker OFF |
|---|---|---|
| Simultaneous questions | 2–4 | **20–40** |
| RSS per worker | ~1.3–1.6 GB | **~350 MB** (torch is never imported) |
| Workers that fit in 8 GB | 2 | **4–6** |
| Answer latency | ~6–12 s | **~3–8 s** |
| `m7i-flex` burst profile | exceeded during a rush | comfortably inside |

The implementation returns `None` from `_load_model()` before the
`sentence_transformers` import, which is why the memory is genuinely reclaimed
rather than merely idle. `rerank()` and `rerank_multi()` then take the same
degradation path they already take for a missing model: documents come back in
hybrid-retrieval order, unscored, and nothing raises.

### What the evidence says

`tools/eval_retrieval.py` was run with reranking off and **every variant
passed** — all gold markers still reached the top-12 on every phrasing. That is
what makes this a supported configuration rather than a foot-gun, and the credit
belongs to the chunking, the hybrid weights and `multi_query_retrieve()`: the
retrieval layer is strong enough to find the evidence on its own.

### What that evidence does *not* say

That eval measures **recall** — whether the right chunk is in the pool. It does
not measure **order within the pool**, and the LLM reads the context top-down.
Two specific things are given up, both of them documented measurements from this
repo:

- **Window-level ranking.** The chunk containing the full program list scores
  `-9.842` as a whole chunk and `+8.711` on its `COURSE OFFERING` window, because
  its first 2000 characters are a reference list and classroom rules. With
  reranking off, that chunk is ordered by embedding similarity over the whole
  chunk.
- **Multi-aspect round-robin.** `rerank_multi()` exists because a three-part
  question pushed its minority ask (CITAS) to rank 13, just past a 12-slot
  cutoff. That merge is part of the reranker; off means off.

So: reach for this when the box is the problem, not as a default. If answers get
vaguer after flipping it, this is the first thing to flip back.

### Telling it apart from a broken model

A deliberate switch-off and missing weights produce **identical behaviour**, so
the startup log distinguishes them explicitly:

```
[startup] Reranker DISABLED (RERANKER_ENABLED=false) — answers keep hybrid
          retrieval order and carry no score.
```

versus the existing `[startup] WARNING: reranker OFF ... fix: python
download_model.py`. One is a configuration, the other is a fault. `docker logs`
should never leave anyone guessing which.

Accepted false values are `false`, `0`, `no`, `off` (any case, surrounding
whitespace ignored). **Anything else leaves reranking on**, including typos —
the safe direction, since the alternative is someone believing they disabled a
3.5 s CPU phase that is in fact still running on every question.

---

## Beyond one box

The ceiling here is one CPU-bound phase sharing a machine with a web server. In
rough order of effort:

1. **Move the reranker out of the request path** — its own service, its own
   instance, scaled independently. `rerank()` is already a pure function of
   (queries, docs), so this is an HTTP boundary and nothing more.
2. **Two app containers behind a load balancer.** `/health` already answers
   `200 OK` for the target group. Note the prerequisite: `STORE_BACKEND=dynamodb`,
   or admin decisions apply to only half the traffic — see
   [SHARED_STORAGE.md](SHARED_STORAGE.md), which exists because that silently
   happened.
3. **Cache answers to repeated questions.** During enrollment week the same
   handful of questions dominate, and `rag/analytics.py` already knows which.

---

## What is tested

`tests/test_capacity.py` (in `python run_tests.py`) covers the properties above
with a fake model, so it needs no weights, no network and no AWS:

- concurrent scoring never exceeds `RERANKER_CONCURRENCY`, via both `rerank()`
  and `rerank_multi()` — the two independent `predict()` call sites
- a queued request is served, not dropped
- a full queue returns retrieval order after the timeout instead of hanging
- slots survive repeated inference failures, so capacity cannot bleed away
- `RERANKER_ENABLED` defaults to **on**, so a missing env var can never silently
  ship unranked answers
- every accepted false spelling (`false`/`0`/`no`/`off`) disables it and every
  unrecognised value does not, because failing safe here means "still ranking"
- disabled returns retrieval order, reaches no model, takes no semaphore slot and
  leaves no `rerank_score` behind — `n/a` in the log is true, `0.0` would not be
- the startup banner distinguishes a deliberate switch-off from broken weights
- the `Dockerfile` still agrees with the code about threads, workers and
  `/dev/shm`

That last one exists because this class of bug is a disagreement between a
config file and an assumption in code, and nothing else in the suite would
notice. Every other suite asks one question at a time — which is also how
development happens, and why unbounded concurrent scoring was invisible until it
was deployed.
