# Capacity

How many students can use this at once, on the box it actually runs on.

The deployment target is a single **`m7i-flex.large`** — 2 vCPU, 8 GB RAM —
running the container from the `Dockerfile` at the repo root. Everything below is
derived from that shape and from the code in `rag/chain.py` and
`rag/reranker.py`, not from a general rule of thumb about Flask.

---

## The answer

Reranking is **off by default** in this deployment (`ENV RERANKER_ENABLED=false`
in the `Dockerfile`), which removes the only CPU-bound phase of a request. The
numbers below are for the shipped configuration; the reranked figures are kept
alongside because they are what the trade is measured against.

| Question | Shipped (reranker off) | With reranker on |
|---|---|---|
| Questions being *processed* at the same instant | **20–40** | 2–4 |
| Students actively chatting (asking, then reading for 20–40 s) | **60–100** | 30–60 |
| Answer latency | **~3–8 s** | ~6–12 s |
| Sessions merely open on the page | **hundreds** | hundreds |
| Point where it starts to feel slow | **~40+ simultaneous questions** | ~8–10 |

For a single campus assistant, 60–100 students mid-conversation is a busy day.

The binding constraint has moved. With the cross-encoder gone the box is no
longer CPU-starved, so the ceiling is now **memory** — every worker holds a torch
runtime whatever the flag says, and that is what fixes the worker count at 3.
The next two sections are the two halves of that.


---

## Where a request's time goes

One question, traced through `rag/chain.py`:

| Phase | Wall clock | Resource | Still there with reranking off? |
|---|---|---|---|
| Optimizer LLM rewrite | 0 s, or ≤2.5 s | network | yes |
| Query embedding (MiniLM, once per probe) | ~20–60 ms × probes | **CPU** | **yes** |
| Pinecone hybrid fan-out (parallel probes) | ~0.5–1 s | network | yes |
| **Cross-encoder rerank** | **~3–4 s** (up to ~16 s multi-part) | **CPU** | **no — removed** |
| Prompt assembly, DynamoDB reads | ~0.2 s | network | yes |
| LLM streaming, first token to last | ~2–8 s | network | yes |
| **Total** | **~3–8 s off / ~6–12 s on** | | |

The optimizer row is 0 s for a self-contained question because `rag/latency.py`
skips it — see the "Answer speed" section of the README.

Almost every row is a socket wait, which is why the `Dockerfile` runs
`--threads 4`: a thread parked on a Pinecone read costs nothing, and without
threads a few slow answers would occupy every worker and queue everyone else at
the front door.

**The embedding row is easy to miss.** It survives `RERANKER_ENABLED=false`,
because the dense half of hybrid retrieval has to turn the query into a vector
locally, and `multi_query_retrieve()` fans out up to 8 probes concurrently
(`max_workers=min(len(queries), 8)`) across the dictation repair, the
Tagalog/Waray variants and the paraphrase. Tens of milliseconds each is
negligible against an LLM stream, but it is not zero, and it is the reason the
torch thread pinning below still matters in the shipped configuration.

With the cross-encoder gone, the per-question CPU cost falls from ~3.5 s to
well under 0.5 s, and throughput stops being a function of core count. That is
the whole of the 2–4 → 20–40 change.


---

## Why memory, not CPU, fixes the worker count

Each gunicorn worker loads its own copy of every model:

| Component | Reranker ON | Reranker OFF (shipped) |
|---|---|---|
| PyTorch + transformers runtime | ~700 MB–1 GB | **~700 MB–1 GB — unchanged** |
| MiniLM-L6 embeddings (384-d) | ~120 MB | ~120 MB |
| `ettin-reranker-32m` cross-encoder | ~180 MB | **0 — skipped** |
| Flask, LangChain, boto3, Pinecone client | ~250 MB | ~250 MB |
| **Per worker** | **~1.3–1.6 GB** | **~1.1–1.4 GB** |

### Turning the reranker off does *not* give the torch memory back

This is worth stating plainly, because the opposite was written in this document
once and a worker count was sized from it.

`rag/reranker.py` genuinely returns from `_load_model()` before constructing the
`CrossEncoder`, so the 180 MB of cross-encoder weights are never allocated. But
`rag/chain.py` does this at **module scope**, in every worker, before any of that
runs:

```python
embeddings = get_local_embeddings()   # HuggingFaceEmbeddings(all-MiniLM-L6-v2)
```

`HuggingFaceEmbeddings` *is* sentence-transformers, which *is* torch. The ~1 GB
runtime is resident whether the flag is on or off. What the flag saves is the
~3.5 s of CPU per question — which is the thing that actually mattered — plus
180 MB, not 1 GB.

So the worker budget: **3 × ~1.3 GB ≈ 3.9 GB**, leaving ~4 GB of headroom on an
8 GB box for page cache, the SSE buffers and ordinary drift. 4 workers (~5.2 GB)
is survivable on a quiet day and puts an OOM kill mid-answer within reach on a
busy one, which is the single worst failure mode available here — the student
sees a dead connection, not a slow answer.

**Why 3 and not 2.** While reranking was on, a third process was actively
harmful: it competed for the same 2 cores during a 3.5 s cross-encoder batch and
made every concurrent answer slower. With that phase gone the remaining work is
nearly all socket wait, so the third worker buys queue headroom instead of
contention. That is why this number moved when the flag flipped, and why it
should move back to 2 if the flag is ever flipped back.

**`--max-requests 400 --max-requests-jitter 50`** rides along with the third
worker. torch's allocator does not return freed memory to the OS, so a
long-lived worker's RSS drifts upward and never comes back down; with less
headroom than before, periodic recycling turns that drift into a non-event. The
jitter matters as much as the limit — without it all three workers reach 400 at
roughly the same moment and restart together, turning a rolling refresh into a
brief total outage.

---

## The fixes applied

### 1. torch threads pinned to 1

`torch` starts one compute thread per core, per process. Three workers on two
cores therefore ask for **six** compute threads, and a measurable slice of every
batch goes to context switching rather than arithmetic. Worse, it is
*load-dependent* — invisible with one developer asking one question, and at its
worst exactly when the system is busiest.

This applies in the shipped, reranker-off configuration too: the MiniLM
embeddings are torch, and the retrieval fan-out embeds once per probe.


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

## The `flex` caveat — and why it stopped mattering

`m7i-flex` is not a fixed-performance instance. It is sized for workloads that
average **up to ~40% CPU**, bursting to 100% when needed.

With reranking **on**, this app violated that profile: the cross-encoder pinned
both vCPUs at 100% for seconds at a time, and during an enrollment-week rush
sustained demand could be pulled back toward baseline — at which point every
figure in the first table roughly halved.

With reranking **off**, which is how this ships, the remaining CPU work is query
embedding measured in tens of milliseconds. The box now sits comfortably inside
the burst profile, so **`m7i-flex.large` is the right instance for this workload
rather than something to migrate off.** That is a real benefit of the switch and
not an incidental one.

If reranking is ever switched back on, the caveat returns with it, and the way
out is in order of preference:

1. `m7i.large` — same 2 vCPU / 8 GB, fixed performance, no burst profile to
   exceed.
2. `m7i.xlarge` — 4 vCPU / 16 GB. Roughly doubles the capacity table and leaves
   room for a fourth worker, which the 8 GB budget above does not.

---

## Turning the knobs

Everything is an env var, so a rush can be handled with `docker run -e` and no
rebuild.

| Variable | Default | Effect |
|---|---|---|
| `RERANKER_ENABLED` | `false` | Off in the shipped image. `true` restores ranking quality at ~3.5 s CPU per question — see below. |
| `RERANKER_CONCURRENCY` | `2` | Requests allowed inside the model at once. Only applies when ranking is on. |

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

## Reranking off: what it bought, and what it did not

`RERANKER_ENABLED=false` removes the only CPU-bound phase in the table above.
This is the configuration the image ships with, and it is the single largest
lever in this document:

| | Reranker ON | Reranker OFF (shipped) |
|---|---|---|
| Simultaneous questions | 2–4 | **20–40** |
| CPU per question | ~3.5 s | **< 0.5 s** |
| RSS per worker | ~1.3–1.6 GB | ~1.1–1.4 GB (**not** 350 MB — see below) |
| Workers that fit in 8 GB | 2 | **3** |
| Answer latency | ~6–12 s | **~3–8 s** |
| `m7i-flex` burst profile | exceeded during a rush | comfortably inside |

`_load_model()` returns `None` before constructing the `CrossEncoder`, so
`rerank()` and `rerank_multi()` take the same degradation path they already take
for a missing model: documents come back in hybrid-retrieval order, unscored,
and nothing raises.

**The memory row deserves its correction in writing.** An earlier version of
this document claimed ~350 MB per worker "because torch is never imported", and
a worker count was sized from that claim. It is wrong: `rag/chain.py` builds
`HuggingFaceEmbeddings` at module scope, which imports sentence-transformers and
therefore torch, in every worker, regardless of this flag. What is saved is the
cross-encoder's ~180 MB and — the part that actually matters — its CPU time. The
per-worker footprint stays north of 1 GB, which is why the worker count went to
3 rather than the 4–6 that the false figure would have allowed.


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
- `RERANKER_ENABLED` defaults to **off**, matching the `Dockerfile` and this
  document — the three drifted apart once and the test is what catches it
- every accepted false spelling (`false`/`0`/`no`/`off`) disables it and every
  unrecognised value does not, so an explicit `RERANKER_ENABLED=true` cannot be
  defeated by a typo
- torch is imported even when reranking is disabled, pinning the memory claim
  above to something executable rather than to a comment

- disabled returns retrieval order, reaches no model, takes no semaphore slot and
  leaves no `rerank_score` behind — `n/a` in the log is true, `0.0` would not be
- the startup banner distinguishes a deliberate switch-off from broken weights
- the `Dockerfile` still agrees with the code about threads, workers,
  `--max-requests` and `/dev/shm`


That last one exists because this class of bug is a disagreement between a
config file and an assumption in code, and nothing else in the suite would
notice. Every other suite asks one question at a time — which is also how
development happens, and why unbounded concurrent scoring was invisible until it
was deployed.
