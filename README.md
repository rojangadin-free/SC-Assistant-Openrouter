# SC-Assistant

A retrieval-augmented chat assistant for **Samar College**. Students ask questions in
plain language — "how do I apply for a scholarship?", "sino ang dean ng education?" —
and the assistant answers from the school's own handbooks and bulletins rather than
from the model's general knowledge.

Built with Flask, Pinecone (hybrid dense + sparse retrieval), a cross-encoder
reranker, and AWS (Cognito, DynamoDB, S3).

---

## Why this is more than a PDF chatbot

A plain RAG loop over a stack of PDFs fails in ways that are hard to see and
embarrassing to demo. The bulk of the code here exists to close those specific gaps,
and each one has its own document under [`docs/`](docs/).

| Problem, in practice | What was built | Doc |
|---|---|---|
| Two documents disagree — the handbook names one Dean of Education, a later page names another — and the model picks whichever chunk ranked higher, confidently | **Data Conflicts.** A CLI extracts facts and reports contradictions; an admin pins the correct value, which is injected into the prompt as authoritative | [DATA_CONFLICTS.md](docs/DATA_CONFLICTS.md) |
| Old and new bulletins both retrieve; the model can't tell which is current | **Document Freshness.** Documents are dated, compared, and the newer source wins — checked *before* indexing | [DOCUMENT_FRESHNESS.md](docs/DOCUMENT_FRESHNESS.md) |
| "I don't have that information" is a dead end for the student and invisible to staff | **Content Gaps.** Refusals are detected, grouped by topic, and listed on an admin dashboard as a to-do list | [CONTENT_GAPS.md](docs/CONTENT_GAPS.md) |
| Students type Taglish and Waray; the corpus is English | **Language support.** Local terms are mapped to English search terms before retrieval | [LANGUAGE_SUPPORT.md](docs/LANGUAGE_SUPPORT.md) |
| "when is enrollment" depends on today's date, which the model has no idea about | **Calendar awareness.** Admin-entered periods, real date arithmetic, deadlines | [ACADEMIC_CALENDAR.md](docs/ACADEMIC_CALENDAR.md) |
| The same process differs for a student vs. a faculty member | **Role-aware answers.** Who is asking selects the right side of a process | [ROLE_AWARE_ANSWERS.md](docs/ROLE_AWARE_ANSWERS.md) |
| An answer with no source is hard to trust and impossible to verify | **Citations.** A "Based on" footer naming document and page | [ANSWER_CITATIONS.md](docs/ANSWER_CITATIONS.md) |
| A wrong answer has nowhere to go | **Feedback & Ask-a-Human.** Votes, plus an escalation queue staff can work through | [ANSWER_QUALITY.md](docs/ANSWER_QUALITY.md) |
| Time-sensitive notices ("classes suspended") don't belong in a PDF | **Announcements.** Live windows, audience targeting, a public banner | [ANNOUNCEMENTS.md](docs/ANNOUNCEMENTS.md) |
| Students are on phones, often on poor connections | **Voice input + installable PWA.** Dictation with a Philippine-English locale, offline shell | [VOICE_AND_PWA.md](docs/VOICE_AND_PWA.md) |
| No idea what students actually ask | **Analytics.** Volume, top topics, refusal rate | [ANALYTICS.md](docs/ANALYTICS.md) |
| Retrieval silently missed whole programs (e.g. SCTI) | **Indexing fixes.** Chunking that respects document structure, inspectable before you index | [INDEXING_FIXES.md](docs/INDEXING_FIXES.md) |
| Admin decisions vanish for half the traffic once there are two containers | **Shared storage.** One seam behind every admin store, file or DynamoDB | [SHARED_STORAGE.md](docs/SHARED_STORAGE.md) |
| A fake progress bar, a hidden Analytics screen, an Overview listing five arbitrary rows as "recent", two screens printing the same satisfaction figure differently, and a reload that always threw you back to the landing screen | **Admin dashboard.** Real per-file indexing progress, the analytics UI wired up, a triage queue that answers "what needs me", one screen stating each figure once, and a reload that keeps you where you were | [ADMIN_DASHBOARD.md](docs/ADMIN_DASHBOARD.md) |
| One developer asking one question is not enrollment week: the CPU-bound reranker had no limit, so eight simultaneous questions made *all eight* slow rather than a few wait | **Capacity.** A bounded queue in front of the model, pinned compute threads, and the concurrency figures the box actually supports | [CAPACITY.md](docs/CAPACITY.md) |




---

## UI development

The interface remains Flask/Jinja with the school's existing green and gold.
`console.css` owns shared visual tokens and components; page styles own their
layouts. The shared college masthead is `templates/partials/brand.html`.

New layout utilities use prefixed Tailwind classes (`ui:`), without Preflight or
a browser CDN. The compiled stylesheet is committed so the Python production
image does not need Node. After changing utility classes, run:

```powershell
npm ci --ignore-scripts
npm run build:ui
```

Commit `sc_assistant/static/css/ui.utilities.css` with its template changes.
For local visual checks, `tools/ui_preview.py` renders the real chat/dashboard
templates using synthetic data. `tools/test_portal_ui.py` exercises that fixture;
`tools/test_signup_ui.py` uses the separate fake-Cognito auth preview. Neither
fixture is evidence of live backend capacity or Cognito configuration.

## Project layout

```
run.py                  start the dev server
store_index.py          (re)build the Pinecone index from data/
run_tests.py            run every test suite in one command
config.py               env vars, model names, index name

sc_assistant/           the Flask app — blueprints, templates, static assets
rag/                    retrieval, reranking, and the feature logic above
src/                    PDF loading, chunking, prompt text
aws/                    Cognito / DynamoDB / S3 wrappers
data/                   the source PDFs

tests/                  25 suites, 1,300+ assertions   -> python run_tests.py




tools/                  dev & ops scripts              -> python tools/<name>.py
docs/                   one .md per feature
```

**Scripts in `tests/` and `tools/` run from the repo root**, not from inside those
folders:

```bash
python run_tests.py                      # every suite
python tests/test_pwa.py                 # one suite
python tools/check_data_conflicts.py     # one tool
```

Each folder has a small `_bootstrap.py` that its files import first. It puts the repo
root on `sys.path` and sets the working directory there, so `from rag.chain import ...`
and relative paths like `data/Samar-College-update.pdf` resolve the same way they did
when every file lived in the root. **If you add a file to either folder, start it with
`import _bootstrap` before any project import.**

---

## Setup

**Requires Python 3.10+** (production runs 3.10 per the `Dockerfile`; developed on 3.14).

```bash
git clone https://github.com/rojangadin-free/SC-Assistant-Openrouter.git
cd SC-Assistant-Openrouter

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

Create a `.env` file in the repo root:

```ini
# --- required ---
PINECONE_API_KEY=xxxxxxxx
OPENROUTER_API_KEY=xxxxxxxx
FLASK_SECRET_KEY=xxxxxxxx

# --- required for login (AWS Cognito) ---
AWS_REGION=us-east-1
COGNITO_USER_POOL_ID=xxxxxxxx
COGNITO_CLIENT_ID=xxxxxxxx
COGNITO_CLIENT_SECRET=xxxxxxxx
AWS_ACCESS_KEY_ID=xxxxxxxx
AWS_SECRET_ACCESS_KEY=xxxxxxxx

# --- optional ---
AGENTROUTER_API_KEY=xxxxxxxx   # second LLM gateway; see "Two providers" below
STORE_BACKEND=file             # or `dynamodb` for multi-container deployments
```

`config.py` raises immediately if `PINECONE_API_KEY` is missing, so a misconfigured
`.env` fails at startup rather than on the first question.

Then fetch the models, build the index, and start the app:

```bash
python download_model.py  # embedding + reranker weights into the local cache (run once per machine)
python store_index.py     # embeds data/*.pdf into Pinecone (run once, and after any PDF change)
python run.py             # http://localhost:8080
```

`download_model.py` is not optional, and skipping it fails quietly rather than
loudly. The cross-encoder that reorders search results is loaded on demand, and
when it cannot be found locally the app catches the failure, keeps the raw
hybrid-retrieval order, and answers anyway — the only symptoms are `score=n/a`
against every document in the retrieval log and noticeably worse answers. Startup
now says so explicitly (`[startup] WARNING: reranker OFF ...`), so check the boot
output on a new machine before assuming the models are there.

**The deployed image ships with reranking off** (`ENV RERANKER_ENABLED=false`),
which removes the only CPU-bound phase of a request and trades ranking quality
for roughly ten times the concurrency on a 2-vCPU box. The startup line is
different in that case (`Reranker DISABLED`, no "fix" suggestion) precisely
because a deliberate switch-off and missing weights otherwise look identical.

Set `RERANKER_ENABLED=true` to trade it back: better ordering within the
retrieved set, at ~3.5 s of CPU per question. Retrieval recall is unaffected
either way — `tools/eval_retrieval.py` passes every variant with it off. See
[CAPACITY.md](docs/CAPACITY.md) for both sides of that trade, including what
ordering actually buys and why the worker count moves with this flag.




### DOCX tables and re-indexing

DOCX ingestion preserves native table rows and their office/person associations,
including vertically merged labels. Markdown table rows are not section headings;
large table chunks repeat their column labels. Empty or unreadable uploads fail
instead of appearing as successfully indexed files. The conflict scanner also
reads native DOCX tables, not just top-level paragraphs.

Explicit program catalogs also retain their collection context when a Word file
uses the same heading level for the catalog, a college, and a degree. Related
entries stay under the catalog until an unrelated section begins; continuation
chunks retain the catalog label. Generation receives the stored section/header
metadata as well as the full passage text. This does not hard-code program names
or infer source priority from a filename.

Readable native DOCX catalog text does not need page OCR. PDF extraction still
uses text-quality checks to select OCR where needed. Empty or failed OCR now
retains available native text, and partial column OCR retains the native fallback.
PDF metadata records `extraction_method` and `ocr_requested` so the path is
inspectable. This is not a guarantee that every scan is legible.

Index writes now use source-aware chunk IDs, so identical passages in a PDF and
a DOCX do not overwrite each other's source metadata. This is not automatic
document-version replacement: already indexed, changed passages are not removed
merely by appending the same filename again.

For a DOCX affected by the earlier table-loss bug, retain the original file,
deploy/restart with the fix, remove the affected document entry through the admin
document manager, and upload it again. That removal also removes its stored S3
copy and catalog entry, so keep the source file available first. Retire an old
PDF only if it should no longer be used, or record the documents' genuine
effective dates. Start a fresh chat when checking the corrected source.

A newer roster can coexist with older editorial captions inside one document;
keep those statements clearly labeled or remove obsolete content in the source.
The application preserves names as written rather than guessing spelling or age
from a filename. DOCX can still process faster than PDF: upload duration is not a
completeness check.

`python tests/test_docx_ingestion.py -v` exercises extraction, chunking, and index
payloads with synthetic documents and mocked services. It does not use a live
index or call OCR/LLM services.

`python tests/test_catalog_ingestion.py -v` checks complete catalog grouping,
college/major continuity, generation metadata, and selective PDF OCR fallbacks.
Re-index affected files after an ingestion change; existing vectors are not
rewritten by changing the application code alone.

### Request-frequency limits

[`sc_assistant/rate_limit.py`](sc_assistant/rate_limit.py) enables request-frequency
limits by default. Each rule uses a fixed window starting with the first request
for that identity. Atomic counters live in a **private SQLite file shared by
Gunicorn workers on one container/host**, not by independent replicas. All workers
must use the same file and stable `FLASK_SECRET_KEY`. `STORE_BACKEND=dynamodb` does
not distribute these counters; independent replicas need a shared network-backed
limiter before scaling.

Authenticated activity outside auth routes is keyed by session `uid` (falling back
to the signed-in user identifier if absent). Guest requests and auth routes use
client-IP budgets. Login also consumes a separate budget for the normalized
(trimmed, case-folded) username/email target **paired with that IP**. Resetting a
session or starting another guest session does not reset the guest IP quota.
Shared campus NAT can put many students behind one address, so guest/auth IP
limits may need tuning; signed-in activity remains user-based.

Exact defaults, in requests per window for each rule/identity:

| Rule | Requests | Window (seconds) | Applies to |
|---|---:|---:|---|
| `api` | 300 | 60 | API/data reads and feedback batch reads |
| `write` | 120 | 60 | Other POST/PUT/PATCH/DELETE requests |
| `chat` | 20 | 60 | Authenticated chat |
| `guest_chat` | 6 | 60 | Guest/unauthenticated chat |
| `login` | 60 | 60 | Login attempts per IP |
| `login_target` | 10 | 600 | Login attempts per normalized target + IP |
| `signup` | 30 | 600 | Signup |
| `code_send` | 20 | 600 | Password-recovery code requests and signup-code resends |
| `verify` | 30 | 60 | Signup confirmation, password reset, and Settings password changes |
| `feedback` | 60 | 60 | Feedback submissions |
| `report` | 10 | 600 | Reports and escalations |
| `upload` | 6 | 300 | Admin document uploads |
| `scan` | 6 | 300 | Conflict scans and freshness upload scans |
| `poll` | 600 | 60 | Upload-status polling |

Configuration:

- `RATE_LIMIT_ENABLED=true` is the default; `false` explicitly disables these
  request-frequency limits, not the separate concurrency/recovery protections.
- `RATE_LIMIT_STORAGE_PATH` is optional. When unset, the file is named
  `<stem>.rate-limits.sqlite3` beside `SC_RUNTIME_DB`, or otherwise beside
  `PASSWORD_RESET_STATE_PATH`. If neither is set, it uses a private directory
  under the system temp directory. Keep it private, writable by the app, and on
  persistent local storage in production.
- `RATE_LIMIT_PROXY_HOPS=0` is the default and does not trust forwarded client IPs.
  Set `1` **only when backend traffic is network-restricted to the trusted ALB**,
  with `X-Forwarded-For` in append mode. This trusts the rightmost address appended
  by the ALB, not a caller-supplied leftmost address. Gunicorn's
  `FORWARDED_ALLOW_IPS` alone is not enough for client-IP attribution.
- `RATE_LIMIT_RULES` is a partial JSON map of recognized rule names to
  `[count, seconds]`; omitted rules retain their defaults. Both numbers must be
  positive integers. Unknown names, malformed JSON, or invalid rule values fail
  startup rather than silently disabling protection.

Example partial override for the guest-chat and login IP budgets:

```ini
RATE_LIMIT_RULES={"guest_chat":[12,60],"login":[120,60]}
```

Quota exhaustion returns HTTP `429` JSON with `"success": false`, `message`,
`error`, `"code": "rate_limited"`, and integer `retry_after` seconds, plus
`Retry-After` and `Cache-Control: no-store` headers. If quota storage fails,
protected work **fails closed with HTTP `503`**, the same fields/headers,
`"code": "rate_limit_storage_unavailable"`, and a three-second retry. These
rejections happen before downstream provider/database/indexing calls. `/health`,
static/PWA resources, `OPTIONS`, and ordinary page navigation are unaffected by
these limits or quota-storage failures. Persisted quota keys are HMAC digests,
not raw IPs, emails, or passwords.

Existing chat concurrency caps and recovery coordination/resend cooldowns remain
separate and unchanged. Request-frequency limits do not replace WAF/network
protection, payload-size limits, or work concurrency caps. See the
[AWS guide](docs/AWS_NEW_ACCOUNT_DEPLOYMENT.md#request-frequency-limits) for the
private volume path and ALB-only configuration.

### Testing on a phone

Voice input and "Add to Home screen" both require a **secure context**. Browsers grant
that to `https://` and to `localhost`, but *not* to `http://192.168.x.x` — so over plain
HTTP on a phone the mic button simply doesn't appear, with no error to explain why:

```bash
python run.py --https     # https://<your-lan-ip>:8443, self-signed
```

---

## Tests

```bash
python run_tests.py
```

25 suites, 1,300+ assertions, **no AWS credentials and no network required** — each





suite redirects its storage to a temp file, so the real `conflict_resolutions.json`
and friends are never touched. Suites run in separate processes because those
redirects have to be set before module import.

Run one suite directly when you're working on a feature:

```bash
python tests/test_conflicts_api.py
python tests/test_freshness.py
python tests/test_latency.py
```

### Answer speed

`rag/latency.py` owns *when* work in the answer path is allowed to happen, and
`tests/test_latency.py` is what keeps it honest. Two things it enforces:

- A self-contained question ("who is the dean of the college of education")
  skips the query-optimizer LLM entirely, because a rewrite cannot find a
  document the student's own words miss. That removes a whole round-trip from
  the blank screen before the first token.
- A follow-up ("what about for transferees?") still waits for the rewrite.
  Searched literally it retrieves the *previous* topic — a wrong answer, which
  is worse than a slow one — so anything ambiguous resolves toward waiting.

When the rewrite is wanted it is no longer waited for *first*: retrieval starts
on the literal question immediately and the rewrite joins as one more probe if
it lands inside `SC_OPTIMIZER_BUDGET_SECONDS` (default 2.5). A late one is
dropped, not cancelled.

Every answer logs where its time went, so the next change here starts from
numbers rather than from "it feels slow":

```
[timing] optimizer=skipped retrieval+rerank=1.31s total=1.34s
```

### What the student sees while waiting

The wait above is now also *legible*. Between pressing Enter and the first token
the typing dots carry a caption of what the pipeline is actually doing:

```
Searching Samar College documents  ->  Reading 34 pages from 3 documents  ->  Writing the answer
```

The captions are published by the code that does the work (`rag/progress.py`,
emitted from `rag/chain.py`), not rotated on a timer in the browser. That
distinction is the whole point: a scripted caption would flash all four states in
300 ms on a fast answer and sit on "Writing the answer" for four seconds on a slow
retrieval — and it would openly contradict the `[timing]` line above it. The page
count is the real retrieved pool, so it agrees with the "Based on" footer.

Two consequences worth knowing:

- The pipeline runs **inside** the SSE generator. It used to run before the
  response was returned, which meant every phase elapsed while the browser was
  still waiting on headers — there was no open connection to send a caption to, so
  three static dots were the only honest UI available. `tests/test_progress.py`
  §6 guards this.
- "Understanding your question" only appears when the optimizer rewrite actually
  runs. For a self-contained question `rag/latency.py` skips it, and announcing a
  phase that was skipped is exactly the dishonesty this avoids.

A caption can never cost an answer: the channel is bounded, lossy, and swallows
its own errors, and `emit()` with no channel is a no-op so `tools/probe_retrieval.py`
and `tools/eval_retrieval.py` keep driving the same graph with no browser attached.



---

## Tools

Data-quality and retrieval-debugging scripts. All are run from the repo root.

| Script | What it does |
|---|---|
| `check_data_conflicts.py` | Extracts facts from the corpus and reports contradictions. Exit code 1 when unresolved, so it can gate a re-index |
| `verify_chunking.py` | Shows how a document *will* be chunked before you index it |
| `probe_candidates.py` | Inspects the recall stage only — what the retriever found |
| `probe_retrieval.py` | Runs the real retrieval + rerank pipeline without calling the chat LLM |
| `eval_retrieval.py` | Regression test for retrieval stability |
| `make_pwa_icons.py` | Regenerates the installed-app icons from the school logo |
| `create_stores_table.py` | Creates the DynamoDB table behind `rag/store.py` and copies local JSON in |
| `create_reports_table.py` | Creates the DynamoDB table for reports |
| `seed_students.py`, `link_students.py`, `student_debug.py` | Dummy student records and Cognito accounts, for development |

A useful pairing before shipping new PDFs:

```bash
python tools/check_data_conflicts.py && python store_index.py
```

---

## Admin

Admin work happens on one page, **`/dashboard`** (plus `/admin/reports/` for generated
reports). Supporting JSON API namespaces include:

| API namespace | Purpose |
|---|---|
| `/admin/conflicts/…` | Pin the correct value when documents disagree |
| `/admin/gaps/…` | Questions the assistant couldn't answer |
| `/admin/feedback/…` | Answer votes and the Ask-a-Human queue |
| `/admin/freshness/…` | Document dates, and the pre-index upload scan |
| `/admin/analytics/…` | Volume, top topics, refusal rate |
| `/admin/reports/` | Generated reports (its own page) |

Upload official academic calendars and notices through **Documents**. The manual
Academic Calendar and Announcements panels and chat announcement banners are removed.
Chat uses retrieved documents for these topics, with the existing effective-date
priority rules; upload order alone does not establish priority.

Existing manual records and their legacy calendar/announcement APIs are preserved,
but chat no longer reads those records or polls `/api/announcements`. Uploading a
document does not create automatic countdowns, scheduled banners, or audience targeting.

---

## Notes on two design choices

**Two LLM providers, deliberately.** The fallback model is served by a *different*
gateway than the primary. AgentRouter fronts requests with a content filter that
rejects some entirely ordinary campus questions — asking about "latin honors" returned
HTTP 400 `content-blocked`. Pointing the fallback at the same gateway means the retry
is rejected by the same rule, and the student sees "Streaming interrupted." with no
answer at all. See `FALLBACK_MODEL_NAME` in `config.py` and
`tests/test_stream_fallback.py`.

**File storage by default, DynamoDB when it matters.** Admin decisions live in JSON
files, which is the right call for one process: no setup, readable with `cat`. It
breaks silently the moment the app runs as two containers — an admin pins a value on
instance A, a student asks instance B, and B answers with the old name. Nothing errors
and nothing is logged. `STORE_BACKEND=dynamodb` moves every store behind a shared
table; the default stays `file` so tests and local development need no cloud account.

---

## Tech stack

Python · Flask · LangChain · Pinecone (hybrid dense + BM25) · cross-encoder reranker ·
OpenRouter / AgentRouter · AWS Cognito, DynamoDB, S3 · Docker · GitHub Actions

---

## Deployment (AWS, via GitHub Actions)

The pipeline builds a Docker image, pushes it to ECR, and runs it on an EC2 instance
registered as a self-hosted runner.

**1. IAM user for deployment** with `AmazonEC2ContainerRegistryFullAccess` and
`AmazonEC2FullAccess`. (Exact policies: [`docs/AWS IAM Policies.txt`](docs/AWS%20IAM%20Policies.txt))

**2. ECR repository** — save the URI, e.g.
`315865595366.dkr.ecr.us-east-1.amazonaws.com/assistant`

**3. EC2 instance** (Ubuntu), then install Docker:

```bash
sudo apt-get update -y && sudo apt-get upgrade -y
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker ubuntu
newgrp docker
```

**4. Register EC2 as a self-hosted runner** —
Settings → Actions → Runners → New self-hosted runner, then run the given commands in
order.

**5. Add GitHub secrets:**

```
AWS_ACCESS_KEY_ID        PINECONE_API_KEY
AWS_SECRET_ACCESS_KEY    OPENROUTER_API_KEY
AWS_DEFAULT_REGION       FLASK_SECRET_KEY
ECR_REPO                 COGNITO_USER_POOL_ID
                         COGNITO_CLIENT_ID
```

The deployment flow: build the image → push to ECR → pull on EC2 → run. `/health`
returns `200 OK` for load-balancer checks.
