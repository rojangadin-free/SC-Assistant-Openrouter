# Capacity and Scaling

The documented deployment target is a 2-vCPU, 8-GB EC2 instance. Actual capacity
must be measured on the deployed app with the chosen models and API account.
The previous 20-40 concurrent-question and 3-8-second estimates were not a
30-user end-to-end benchmark and must not be treated as guarantees.

## What the server provides

`gunicorn.conf.py` defaults to **3 workers with 12 threads each**, or 36 HTTP
request-handling slots. An SSE answer occupies a handler until it finishes.
More slots do not create more CPU or upstream model capacity.

Every web worker still loads MiniLM embeddings and PyTorch. Disabling reranking
does not remove that memory or the query-embedding CPU work. Keep the process
count modest on 8 GB and measure resident memory, CPU, errors and queue latency.
The optional cross-encoder remains disabled by default. Enable it only after
evaluating both quality and performance on the target host.

## App-level safeguards

- RAG graphs are stateless. Each request receives history from shared storage;
  authenticated histories come from DynamoDB and guests have isolated expiring
  histories. No worker-local checkpoint retains every prompt or image.
- Shared preparation, retrieval and pipeline pools have fixed worker and queue
  limits. Cancelled queued work retains capacity until removed from the queue.
- Local query embedding inference is serialized per worker and vectors are
  cached. Repeated retrieval uses a bounded, single-flight cache with copied
  metadata so one request's reranking does not change another request's results.
- Only first-turn query rewrites without history or student records can be
  reused. Private answers and personal records are not globally cached.
- Document context appears once in the answer prompt. Auxiliary history-summary
  inputs are bounded and summary capacity is optional rather than a mandatory
  wait behind other students' questions.
- Shared JSON stores use consistent DynamoDB reads, version-checked writes and
  callback replay on conflicts. Failed reads do not overwrite data with defaults.
  Local file mode uses process locks and atomic replacement.
- Feedback is a bounded regression sample with a byte budget as well as a count
  budget. Other shared blobs reject writes beyond their safe item-size budget.
  This is not an unlimited per-record database; very large admin datasets need
  per-record storage before scaling further.
- Guest history defaults to 40 messages, 120 KB and one hour of inactivity.
  New Chat and Sign Out clear it. Enable DynamoDB TTL for physical expiration;
  logical expiry is enforced even before TTL is enabled.

## Durable indexing

`/upload` returns 202 only after inputs and a queued task are stored. In shared
mode, inputs use the existing S3 bucket and task/progress state uses the existing
`SCAssistantStores` table. A separate worker claims a time-limited lease. Only one
indexing job runs globally, avoiding conflicting vocabulary updates and CPU
spikes. Expired claims recover after a crash, with a bounded number of attempts.

Run the worker separately in local development:

```powershell
python tools/index_worker.py
```

The deployment script starts a project-owned index-worker container separately
from Gunicorn. It loads embedding models lazily when a task is available.
Terminated work retains its staged input for recovery. Completed or terminal
failed tasks clean up their staging objects; cleanup failures are retried.

Indexing is at-least-once, not a transaction spanning Pinecone, S3 and DynamoDB.
Replacement vectors use stable source-specific IDs and retry/replacement removes
obsolete source passages after extraction. During a replacement, or after a
permanent indexing failure, source availability can be partial until recovery or
re-upload completes. Avoid corpus rebuilds during the beta, and do not run a
manual rebuild concurrently with the indexing worker.

BM25 and learned spelling vocabulary are published as immutable, checksummed S3
objects with a versioned shared manifest. Web workers refresh the artifacts and
invalidate retrieval caches when the generation changes. Unavailable/corrupt
downloads retain usable local artifacts; mutations and worker refreshes fail
closed rather than silently discard learned state.

For the first deployment of an already-built local corpus, the learned
`spell_vocab.json` is gitignored and therefore is not present in a clean GitHub
checkout. Preview the existing files with
`python tools/sync_retrieval_artifacts.py`, then use `--publish` only after
confirming they match the configured Pinecone index. This uploads the two files
and publishes their shared manifest; it does not reindex or call a model.
The command refuses active indexing and concurrent manifest changes. Web workers
pick up the generation on subsequent requests after the refresh interval.

## Deployment

The GitHub workflow runs offline tests, publishes an image tagged with the commit
SHA, then calls `tools/deploy.sh`. It no longer stops every container or prunes
other services' images, volumes, networks or system files.

The single-host rollout uses a stable streaming-aware Nginx proxy on port 8080:

1. Stop the old index worker gracefully to release memory.
2. Start the candidate backend with **one** web worker and check `/ready`.
3. Switch the proxy and drain the old backend with a bounded graceful timeout.
4. Add workers one at a time, checking their ready markers. A mounted count file
   preserves the desired count across container restarts.
5. Start the durable index worker and retain the old backend for rollback.

This avoids overlapping two full sets of embedding workers on 8 GB. Capacity is
temporarily lower during the one-worker drain interval. Normal existing streams
can finish, but requests exceeding the configured drain deadline can be cut off.
The first migration from the old direct `rag-app-container:8080` requires a
one-time bounded drain before the proxy binds that port. Wait for legacy uploads
to finish before that migration. A rollback that must restart old model workers
can also have a short interruption.

The script requires Linux, Docker, Python 3 and `flock` on the existing host. It
does not provision instances, load balancers, IAM policies or certificates.
Retain the existing HTTPS termination in front of port 8080.

The inner proxy must forward `X-Accel-Buffering` so an outer HTTPS Nginx proxy
also sees the no-buffer instruction. If that outer proxy explicitly ignores the
header, its `/chat/get` location also needs `proxy_buffering off`. The waiting
caption appears before response headers; subsequent captions reflect actual
pipeline events rather than an animated sequence of invented phases.

`/health` remains a cheap liveness endpoint. `/ready` checks non-default session
configuration and shared-store access in production, with cached probes. It does
not run paid model requests or prove wallet balance, provider throughput or
answer quality.

## One-time retention setup

The application IAM role needs access to all relevant store keys, including
`guest_history:*`, `index_jobs` and `retrieval_artifacts`, plus the existing S3
bucket's `_sc_assistant/indexing/` and `_sc_assistant/artifacts/` prefixes.
No new table or bucket is required.

Preview the setup without contacting AWS:

```powershell
python tools/setup_scaling.py
```

An authorized operator can run `--apply` to enable table TTL on `expires_at` and
add a seven-day lifecycle rule for staged indexing inputs, preserving other
lifecycle rules. `--on-demand` additionally opts into changing the table billing
mode; review the cost before using it. These commands were not executed against
production during implementation. Set the same region/table/bucket environment
variables as the app before applying.

## Load testing

Offline regression tests are not a live 30-user capacity result.

```powershell
python tools/loadtest_chat.py --users 30 --base-url https://your-app.example
```

The command above is a dry run. No requests or credentials files are read until
`--allow-paid-traffic` is supplied. A live test spends model credits and Pinecone
reads and creates temporary guest histories. Test 5, 10, 20, then 30 users in a
staging environment before coordinating a synchronized beta burst.

The tool uses independent cookie jars, tests real `/chat/get` SSE completion and
reports first-text/completion p50 and p95, failures, HTTP statuses and expected
completion count. It rejects blank, interrupted and error streams. Optional
`--session-cookies` accepts a JSON array of test-account session cookies for
authenticated testing; keep it private. Guest results do not measure Cognito
login throughput or personal-record lookup overhead.

Watch CPU, resident memory, worker restarts, 429/402 errors, completed responses
and latency, not just whether the homepage opens. Multiple OpenRouter keys do not
increase account/provider capacity; adequate credits and model throughput still
matter. Both configured answer models currently use the same OpenRouter account.

## Main settings

| Setting | Default | Meaning |
|---|---|---|
| `GUNICORN_WORKERS` | 3 | Embedding-loaded web processes |
| `GUNICORN_THREADS` | 12 | HTTP handlers per worker |
| `GUNICORN_GRACEFUL_TIMEOUT` | 120 | Drain deadline in seconds |
| `RAG_RETRIEVAL_WORKERS` | 4 | Shared retrieval workers per process |
| `RAG_PREPARATION_WORKERS` | 4 | Shared preparation workers per process |
| `RAG_PIPELINE_WORKERS` | 12 | Context-building workers per process |
| `RAG_QUEUE_TIMEOUT` | 10 | Maximum queue admission wait |
| `RAG_PIPELINE_TIMEOUT` | 90 | Maximum streamed context-preparation wait |
| `PINECONE_REQUEST_TIMEOUT` | 20 | Socket timeout per retrieval/index request |
| `LLM_STREAM_CONCURRENCY` | 12 | Active answer streams per process |
| `LLM_AUX_CONCURRENCY` | 2 | Active auxiliary model calls per process |
| `LLM_REQUEST_TIMEOUT` | 30 | Answer-client request timeout |
| `LLM_MAX_RETRIES` | 1 | SDK retries per model request |
| `LLM_MAX_TOKENS` | 3072 | Answer token cap; evaluate truncation/quality |
| `RETRIEVAL_CACHE_TTL` | 30 | Retrieval reuse window in seconds |
| `ARTIFACT_REFRESH_SECONDS` | 10 | Shared artifact refresh interval |
| `INDEX_JOB_LEASE_SECONDS` | 120 | Recoverable worker lease |
| `INDEX_JOB_MAX_ATTEMPTS` | 3 | Maximum recovered task attempts |

All concurrency limits are per process unless stated otherwise. Adding app
replicas multiplies their potential upstream demand. Shared state makes horizontal
scaling possible, but autoscaling infrastructure and API capacity must be
configured and measured separately. Large permanent conversation/admin datasets
also need paging/per-record storage rather than unlimited DynamoDB JSON blobs.
