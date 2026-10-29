# Capacity

## Target, not a throughput guarantee

The target remains one `m7i-flex.large` (2 vCPU, 8 GB RAM), with no additional
server or Redis service. The deployment is configured to admit **30 simultaneous
text questions**. Actual response times depend on CPU/RAM, question complexity,
Pinecone latency, and the LLM provider's concurrent-request and token limits.

Earlier versions of this document claimed 20-40 simultaneous answers and 3-8
second latency without a representative live load test. Those were estimates,
not established capacity, and should not be used for beta-test planning.

## Current limits

| Resource | Default | Scope |
|---|---|---|
| Gunicorn | 3 `gthread` workers, 12 threads each | 36 HTTP request slots total |
| Admitted chat turns | `CHAT_MAX_ACTIVE=30` | Shared across workers in this container |
| Image turns | `CHAT_MAX_IMAGES=2` | Included in the same 30-turn limit |
| Duplicate turns | One per conversation/session | A second overlapping turn gets HTTP 409 |
| Answer preparation/stream jobs | `ANSWER_WORKERS=12`, no pending queue | Per process |
| Retrieval jobs | `RETRIEVAL_WORKERS=4`, 24 pending slots | Per process; 12 running jobs across 3 workers |
| Retrieval fairness | At most 2 probes in flight per question | Every original query variant is retained |
| MiniLM inference | One call at a time per model instance | Per process; at most 3 across 3 workers |
| Query-vector cache | `QUERY_EMBEDDING_CACHE_SIZE=512` | Bounded LRU per process |
| Preparation/persistence | `PREPARATION_WORKERS=4`, 24 pending slots | Per process |
| Optional optimizer | `OPTIMIZER_WORKERS=2`, no pending queue | Per process; skipped when busy |
| Response budget | `CHAT_TIMEOUT_SECONDS=90` | Covers preparation, generation and persistence waits |
| Model output | `CHAT_MAX_OUTPUT_TOKENS=4096` | Per answer model call; route also bounds characters |
| Retrieval wait | `RETRIEVAL_TIMEOUT_SECONDS=30` | Also bounded by the remaining request budget |
| Heartbeat | `CHAT_HEARTBEAT_SECONDS=5` | During preparation, provider waits and persistence |
| Image upload | `CHAT_MAX_UPLOAD_MB=8`, at most 12 megapixels | Per chat request |

Slots are not additional CPU cores. Do not increase the process count simply to
accept more sockets: every worker still loads PyTorch and MiniLM even when
`RERANKER_ENABLED=false`. Numerical libraries remain pinned to one compute thread.
Measure RSS and CPU on the deployed image before changing these settings.

The chat admission limit leaves some aggregate HTTP headroom, not dedicated
login/admin threads. A hot worker or saturated dependency can still reject work.
Overload returns HTTP 429 with `Retry-After: 3`, or an SSE error if a worker-local
pool fills after streaming begins. There is no unbounded waiting room.

Timed-out running jobs retain their executor slots until they actually finish.
Cancelled queued jobs retain their slots until their queue entries are consumed,
so cancellation cannot bypass the memory bound. Python cannot forcibly stop an
in-flight native model or SDK call. Leases
also expire after a crashed worker; they are not released just to pretend a slow
job stopped. DynamoDB resources are thread-local, with bounded connection/read
timeouts and retries. Provider retries are reduced to avoid retry amplification.

## Less work, same evidence

- Retrieved document text is inserted into the prompt once, not twice.
- Date labels and freshness guidance share one per-turn metadata snapshot.
- Sparse/dense searches reuse cached query vectors; the model serializes local
  inference rather than executing many CPU batches at once.
- Cache keys are hashes. Only query vectors are cached, never personalized
  answers or student records. Vectors belong to a fixed model instance and do not
  need invalidation when document contents change.
- The spelling repair remains the working question for optimization, retrieval,
  reranking and generation. The original displayed/persisted question is kept.
- The existing 15-document context window and multilingual/multipart variants are
  preserved. Reranking remains disabled by default; enabling it needs a new load
  test and answer-quality comparison.

## Shared state and streaming

`rag/chat_runtime.py` uses SQLite/WAL for cross-worker leases and temporary guest
history. `SC_RUNTIME_DB=/tmp/sc-assistant-runtime.sqlite3` is shared by the three
workers inside the container, not by separate servers. Worker recycling retains
guest history; replacing the container does not unless this path is mounted.
Guest history defaults to a 2-hour TTL, 300 sessions and 20 recent messages, with a
per-session byte budget. These are temporary sessions, not durable accounts.

Authenticated conversations remain in DynamoDB and are explicitly supplied to
each graph invocation. The graph no longer keeps an unbounded, process-local
checkpoint copy of every conversation, document context and uploaded image.

Admin stores use consistent reads and conditional revisions, retrying mutations
on conflicts. Existing unversioned records are upgraded on their next write.
File-mode mutations have cross-process locks and unique atomic temporary files.
Read failures/corrupt stores cannot be treated as empty stores during mutation.
Whole-blob DynamoDB storage still has a 400 KB item limit; oversized writes fail
rather than silently discarding additional history. Moving high-volume stores to
individual DynamoDB rows is future work, not part of this same-box change.

SSE `done` is emitted only after a nonempty, completed answer and successful
history persistence. Mid-stream failures retain visible partial text but send an
error, not a successful completion. The client also detects missing completion.
Disconnects signal cancellation and release admission only when associated work
has finished. A write already in flight cannot be rolled back by disconnecting.

## Deployment precautions

- Gunicorn has `--timeout 120` and `--graceful-timeout 120`. The first is a worker
  liveness timeout, not a per-request deadline. The graceful timeout allows the
  normal 90-second response budget during worker recycling.
- The Docker image defaults `WEB_INDEXING_ENABLED=false`. Uploads get a clear 503
  while paused, so OCR/indexing does not compete with the beta. Enable it outside
  testing hours; this is an operator setting, not a durable indexing queue.
- Retain proxy streaming settings: no response buffering/transformation and an
  idle timeout longer than the heartbeat interval. Never deploy during a test.
- Both configured answer models currently use OpenRouter. A fallback model does
  not bypass the gateway's shared quota or account balance.
- The SQLite admission/history design is single-container. Multiple independent
  replicas require a shared admission/session design before claiming one global
  30-question limit. Durable admin state must still use `STORE_BACKEND=dynamodb`.

## Regression tests

```powershell
python run_tests.py
```

The runner includes bounded-pool/cache tests, actual chat-route tests with fake
external dependencies,30-request admission/isolation checks, real multi-process
file/guest-store tests, conflicting DynamoDB-write tests, and SSE-parser checks.
It also retains the spelling, freshness, citations, streaming and admin suites.
These establish application behavior, not deployed model throughput.

## Offline HTTP load test

Install Locust in an isolated development environment, not the production image:

```powershell
python -m pip install -r requirements-dev.txt
```

Start the test-only server in a separate terminal:

```powershell
python tests/fixtures/chat_load_stub.py --port 8765 --first-token-delay 0.15 --done-delay 0.25
```

Then run one synchronized question from each of 30 independent sessions:

```powershell
python -m locust -f tools/loadtest_chat.py --headless -u 30 -r 30 --run-time 30s --host http://127.0.0.1:8765 --chat-guest
```

This is **transport/tool validation only**, not a benchmark of Flask/Gunicorn
with real embeddings, Pinecone or an LLM. The fixture health marker reports
distinct sessions, peak active requests and completed responses. Modes such as
`--mode missing-done`, `--mode error` and `--mode disconnect` must fail the test,
even when the HTTP status is 200. `--chunk-size 3` tests split frames.

Use the `SSE /chat/get complete` metric for completed answers and `SSE-TTFT` for
first-answer-token latency. `AUTH` is separate. Locust's Aggregate combines these
different measurements and is not answer throughput. Each user asks once by
default; the run then exits automatically. `--chat-turns 2` makes 60 requests and
exercises paired followups. Questions, credentials and answer text are not logged.

## Authorized live rehearsal

Live testing is deliberately blocked unless `ALLOW_LIVE=1` is explicitly set.
Even localhost must expose the fixture marker unless live mode is authorized.
Real remote targets require HTTPS. Use pre-existing, distinct test accounts via
`--chat-accounts` and an external JSON file containing `email`/`password` entries;
never commit credentials or use real student records. Do not run the generator
on the small production EC2 instance itself.

After approval, test 5, 10, 20, then 30 sessions; repeat with varied questions, both
cold and warm caches, and followups. Require completed streams, correct session
isolation, intact feedback/history, no unexpected worker restarts, and acceptable
p95 first-token and completion times. Inspect CPU/RAM and 429/5xx responses.
Do not substitute `/health` checks or repeated identical cached queries for this
rehearsal. Real LLM calls consume account quota and may incur costs.
