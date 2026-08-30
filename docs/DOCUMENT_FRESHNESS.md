# Document Freshness & the Pre-Index Upload Scan

## The problem this closes

`data/Samar-College-update.pdf` names **Jacqueline Montalis** as dean of the
College of Education. `data/Samar-College-2024.pdf` names **Dr. Nimfa T.
Torremoro** for the same college. Both are indexed, both are retrieved, and the
assistant answers with whichever chunk happened to rank higher — or worse, lists
both and asks the student to decide.

The **Data Conflicts** screen (`DATA_CONFLICTS.md`) already fixes this *once an
admin has ruled on it*: a resolved conflict becomes a `<verified_facts>` line
that overrides everything. That is an absolute answer, and it is the right tool
for a fact somebody has actually looked at.

Two holes were left:

1. **Nobody has ruled yet.** Between the day a contradicting file is indexed and
   the day an admin resolves it, the assistant is free to pick the stale value.
   The corpus contains no signal about which page superseded which.
2. **The contradiction is discovered too late.** The old flow was: upload →
   index → a student gets a wrong answer → somebody notices → resolve. Every
   step after "index" happens while students are being misinformed.

This feature addresses both: documents get **effective dates**, and uploads get
**scanned before a single vector is written**.

---

## Part 1 — Effective dates as a tie-breaker

`rag/freshness.py` records one date per document and, at answer time, tells the
model which of the sources it is quoting is newer:

```
<document_dates>
The sources below are dated. When two sources disagree on a fact, prefer the one
with the LATER effective date and say which document you followed.
- Samar-College-update.pdf — effective 2025-08-01 (NEWEST)
- Samar-College-2024.pdf — effective 2024-06-01 (OLDEST)
</document_dates>
```

That block is **emitted only when at least two cited sources have different known
dates**. An ordinary question, or a question answered out of one file, pays
nothing — no tokens, no block, no instruction.

### Where the date comes from

`infer_effective_date()` tries three sources, in this order of trust:

| Priority | Source | Example | Confidence |
|---|---|---|---|
| 1 | **Admin override** | set on the Freshness screen | `admin` |
| 2 | **The document's own text** (first ~4000 chars) | "Effective August 2025", "Revised: June 2024", "S.Y. 2025-2026" | `high` / `medium` |
| 3 | **The filename** | `Samar-College-2024.pdf` | `low` |

Two deliberate refusals:

- **An undated document gets `None`, never today's date.** Defaulting to "now"
  would silently make every unlabelled file the newest thing in the corpus and
  hand it authority over the real handbook.
- **A year buried in prose is not a document date.** "Enrollment runs from June 1
  to June 15, 2025" and "Room 2024 is on the second floor" are both ignored; the
  patterns require a labelling word (*effective*, *revised*, *approved*,
  *updated as of*, *S.Y.*).

### Why this is a *fallback*, not the main mechanism

A date says "this page is newer". It does not say "this value is right". A newer
document can contain a typo, and `<verified_facts>` exists precisely for the case
where a human has checked. So the prompt ranks them:

```
verified_facts  (an admin decided)      →  absolute
document_dates  (one file is newer)     →  tie-breaker when nobody has decided
retrieval order (a chunk scored higher) →  no longer decides anything
```

The rule in `src/prompt.py` is scoped tightly on purpose: it applies **only where
two sources actually disagree**. Without that limit, telling a model that one
source is "older" invites it to hedge on facts nothing contradicts — the 2024
handbook is still the only source for most of the corpus, and it must not become
suspect merely because a two-page update PDF exists.

---

## Part 2 — The pre-index scan (the important half)

`POST /admin/freshness/api/scan-upload` takes the file the admin is about to
upload and answers one question: **what would this contradict?**

```
Report for Samar-College-update.pdf
  effective 2025-08-01  (read from the document text: "Effective August 2025")
  pages scanned: 2
  conflicts: 1

  dean of College of Education
    incoming  Jacqueline Montalis            (Samar-College-update.pdf, p.2)
    existing  Dr. Nimfa T. Torremoro         (Samar-College-2024.pdf, p.15)
    newer     Samar-College-update.pdf
    recommend pin "Jacqueline Montalis" — the upload is the newer document
```

Nothing is written: no vectors, no S3 object, no date record. The scan is a **dry
run**, which is the whole point — the admin decides while cancelling is still
free.

### Why it is a separate call from `/admin/upload`

It could have been folded into the upload route as a "warn and continue" step. It
is not, because *a warning attached to a completed action is a warning nobody
acts on*: by the time it appears, the file is indexed, students are already
getting the contradictory answer, and every remaining option is cleanup.

Keeping the scan as its own call also leaves the existing upload route untouched
for the ~95% of files that contradict nothing.

### Why `existing_text` is a parameter, not a Pinecone query

The scan compares against text the caller supplies rather than querying the live
index. A data-quality check that requires the vector store to be reachable is a
check that stops working exactly when the corpus is in trouble — and it makes the
whole path untestable without live credentials.

---

## Endpoints

| Method | Route | Purpose |
|---|---|---|
| GET | `/admin/freshness/api/list` | every document, newest first, with an `undated` count |
| POST | `/admin/freshness/api/set-date` | record or correct one date (`YYYY-MM-DD`) |
| POST | `/admin/freshness/api/delete` | forget a document's date |
| POST | `/admin/freshness/api/scan-upload` | dry-run a file **before** indexing it |
| GET  | `/admin/freshness/api/preview?sources=a.pdf,b.pdf` | the exact block the model would receive |

All five are admin-only. Write access here is write access to the answers
students receive, so an anonymous or student session gets a `403` and nothing is
stored.

`set-date` rejects a non-ISO date with a `400` that names the expected format,
rather than accepting it and silently storing nothing — a date that looks saved
but is not is how a corpus quietly goes back to guessing.

`/api/preview` exists so an admin can confirm the date they set actually reaches
the prompt. When no block applies it says *why* ("fewer than two of these sources
have a known, different effective date"), because an empty box has two very
different causes and an admin cannot tell them apart by staring at it.

---

## Storage

One row per document in the shared store (`rag/store.py`), so it works on a file
in development and DynamoDB in production, and every worker sees the same dates
(`SHARED_STORAGE.md`):

```json
{
  "docs": {
    "samar-college-update.pdf": {
      "filename": "Samar-College-update.pdf",
      "effective_date": "2025-08-01",
      "confidence": "admin",
      "reason": "set by an administrator",
      "set_by": "admin@samarcollege.edu",
      "note": "deans update",
      "updated_at": "2026-08-30T08:14:02+00:00"
    }
  }
}
```

Keys are lowercased basenames, so `s3://bucket/docs/Handbook.pdf`,
`C:\upload\handbook.pdf` and `handbook.pdf` are one document rather than three.
`set_by` is recorded because a wrong date needs a name attached to it — otherwise
nobody can be asked why the assistant now prefers the older file.

---

## Tests

```
python tests/test_freshness.py       # 106 checks — date parsing, ranking, the block
python tests/test_freshness_api.py   #  36 checks — endpoints, authz, the dry run
python run_tests.py            # all 15 suites
```

`test_freshness.py` covers what must *not* happen as heavily as what must: an
undated document does not silently become "today", a year inside a longer digit
run is not a year, `Feb 30` degrades to the month instead of raising, and a
single source never produces a block.

`test_freshness_api.py` builds a real two-page PDF in memory (via `reportlab`,
skipped with a printed notice when it is absent) so the scan path runs the actual
`PyPDFLoader` — a fake byte string would only prove that error handling works. It
then asserts the College of Education contradiction is found, that the upload is
correctly identified as newer, and that **scanning recorded nothing**: a cancelled
upload must not have changed how the assistant ranks its sources.

---

## Operating it

1. Open the Freshness screen and set dates on the documents you already have.
   Start with any file the list flags as **undated** — an undated file can neither
   win nor lose a comparison, so it keeps contradicting its neighbours silently.
2. Before uploading a new file, run the scan. If it reports a conflict, resolve it
   on the Data Conflicts screen (that gives you an absolute `<verified_facts>`
   answer), then upload.
3. If a scan reports no conflicts, upload as usual and set the file's date.

The two features are complementary and the order matters: **dates keep the
assistant sensible until a human rules; a resolution makes it certain afterwards.**
