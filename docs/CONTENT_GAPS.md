# Content Gaps — turning "I don't have that information" into a to-do list

## The problem this solves

`DATA_CONFLICTS.md` handles answers that are **wrong** (two deans for one college). This handles the
other failure mode: answers that are **missing**.

The system prompt correctly forces strict grounding — if the retrieved pages don't contain the
answer, the assistant says so instead of inventing one. That is the right behaviour, but as built the
story ended there:

```
student: "Is there a shuttle service?"
bot:     "I do not have that information in my documents."
         ← student leaves with nothing
         ← nobody ever learns that 30 other students asked the same thing
```

The knowledge that a topic is missing existed for exactly one second and then was thrown away. So
deciding what to upload next was guesswork.

## What was built

Every refusal is now recorded, grouped, and ranked.

```
chat.py  ──▶ looks_unanswered(reply)?  ──▶ record_gap(question)
                                              │
                                              ▼
                                    content_gaps.json
                                              │
                                              ▼
                            Admin ▸ Dashboard ▸ Content Gaps
                        "Shuttle service — asked 34× — needs a document"
```

The admin opens one screen and sees, in priority order, exactly what students want to know that no
uploaded document answers.

### Files

| File | Role |
|------|------|
| `rag/gaps.py` | detection, grouping, storage |
| `sc_assistant/chat.py` | logs the gap right after the reply finishes streaming |
| `sc_assistant/admin_gaps.py` | admin JSON API (`/admin/gaps/api/*`) |
| `sc_assistant/templates/dashboard.html` | the Content Gaps section |
| `sc_assistant/templates/partials/sidebar.html` | sidebar entry + badge |
| `test_gaps.py` | 31 unit assertions (storage, detection, grouping) |
| `test_gaps_api.py` | 26 integration assertions (routes, auth, every button) |


## Three decisions worth explaining

**1. Detection is text-based, not a second LLM call.**
Asking a model "did you actually answer?" after every turn would double the cost and latency of every
message in the system. But the refusal wording isn't arbitrary — *our own system prompt dictates it*.
Matching our own instructions is both cheaper and more reliable than asking a model to grade itself.

`looks_unanswered()` has three guards, because a noisy queue is a queue nobody reads:

- a `[SOURCE: ...]` citation ⇒ it answered, ignore;
- longer than 400 characters ⇒ real answers are longer than refusals, ignore;
- the refusal phrase appears past the halfway point ⇒ it's a caveat inside a real answer
  (*"the fee is ₱500, though the graduate table is not specified"*), ignore.

**2. Grouping is by word stems, not raw text.**
Without grouping, the admin would see 40 near-identical rows and learn nothing:

```
"How do I enroll?"          ┐
"paano po ba mag-enroll?"   ├─▶  one row: "enroll" — asked 3×
"Enrollment?"               ┘
```

Stopwords and Filipino particles (`po`, `ba`, `paano`, `mag-`) are dropped, the remaining words are
stemmed and sorted, and that string is the group key. Word order stops mattering, so *"requirements
for enrollment"* and *"enrollment requirements"* also collapse into one row. No embeddings, no extra
dependency, works offline.

Note that `"how to enroll"` and `"enrollment requirements"` stay **separate** — the second word makes
it a narrower question, and merging them would hide what the student actually needed.

**3. "Resolved" is not a mute button.**
When the admin marks a topic covered and a student asks it *again*, `record_gap()` reopens the row and
notes why:

> `[Asked again after being marked resolved.]`

A fix that didn't actually work becomes visible instead of silently sitting in a "done" pile. This is
the single most useful behaviour in the feature — it closes the loop.

## Using it

1. **Admin ▸ Content Gaps.** Rows are sorted most-asked first; the sidebar badge counts open topics.
2. **Expand "How students phrased it"** before uploading anything. The exact wording is the difference
   between uploading the right page and uploading something adjacent to it.
3. **Upload a document** covering the topic (File Uploads), then **Mark as covered** with an optional
   note recording which document now answers it.
4. **Delete** rows that are junk (test questions, someone asking about the weather). If a real student
   asks again, the row comes back on its own.

## Verifying

```bash
python run_tests.py        # both features, 3 suites, 82 assertions
python tests/test_gaps.py        # storage, refusal detection, grouping
python tests/test_gaps_api.py    # the endpoints the dashboard calls
```

Every suite redirects storage to a temp file, so the real `content_gaps.json` is untouched and no AWS
credentials are needed.

The unit suite covers refusal detection — including the false positives it must *not* fire on, which
is the part that keeps the queue trustworthy — plus cross-language grouping, counting, stats and the
resolve/reopen/delete cycle. The API suite covers the parts a unit test can't see: that the blueprint
is actually mounted, that a logged-in student gets `403` (a student must not be able to delete the
evidence that their question failed), and that each button in the UI maps to an endpoint that works,
including the auto-reopen behaviour over HTTP.


## Storage

A JSON file, same pattern as `conflict_resolutions.json`:

- `content_gaps.json`, overridable via `CONTENT_GAPS_FILE`
- write is atomic (temp file + `os.replace`), guarded by a lock
- capped at 500 topics × 5 example phrasings — this is a prioritisation tool, not an audit trail
- git-ignored: it is per-deployment runtime state

`record_gap()` swallows its own exceptions by design. It runs inside the chat streaming path, and a
logging failure must never cost a student the answer they already received.

To share the log across multiple instances, swap `_load`/`_save` for DynamoDB — nothing else changes.
