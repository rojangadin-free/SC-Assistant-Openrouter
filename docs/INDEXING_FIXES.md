# Data Indexing Fixes — "SCTI programs are not retrieved"

## The bug

Asking *"what are the programs offered?"* returned the degree programs but **never the SCTI
(Samar College Technological Institute) training offerings**.

The text was extracted fine — the problem was **where the chunk boundary landed**.

In `Samar-College-update.pdf` the `COURSE OFFERING` list spans a page break:

| Page | Content |
|------|---------|
| 14 | `COURSE OFFERING` → CGS, CoEd, CITAS, CBM, CoCJE, CLAS |
| 15 | **SCTI + Training Offerings** (Cookery NC II, CSS NC II, DIT), Basic Education, `DEANS UPDATE`, dean names |

`process_pdf()` emits **one Document per page** and the chunker closed the chunk at the end of
page 14. So:

* The chunk that wins retrieval for *"programs offered"* (page 14) **did not contain SCTI**.
* The chunk that contained SCTI (page 15) was dominated by *dean names + basic education*, so its
  embedding did not look like "programs offered" at all.

Four supporting defects made it worse:

1. **`[SECTION: ...]` headers never worked.** `smart_chunking()` only tested for a heading when
   `len(para) < 100`, but `collapse_soft_line_breaks()` collapsed *every* newline into a space,
   turning each page into one ~1300-char paragraph. Every chunk was therefore tagged
   `General College Information` — `COURSE OFFERING`, `DEANS UPDATE`, `Training Offerings` were
   never recorded.
2. **`Page 15 of 48` survived cleaning** and was embedded as noise.
3. **Retrieval was dense-heavy** (`weights=[0.2, 0.8]`), but `SCTI`, `NC II`, `Cookery`, `BMMA` are
   rare exact tokens where BM25 is the strong signal.
4. **Shallow candidate pool** (`top_k=10`, `FINAL_TOP_K=10`, no reranker) while the base handbook
   mentions SCTI/Cookery/Diploma on ~12 pages — the old handbook crowded out the new PDF.

---

## What changed

### `src/helper.py`

| Function | Change |
|----------|--------|
| `remove_headers_footers()` | Strips `Page N of M`, `Page N`, `p. N`, `Samar Colleges, Inc. <n>`, and separator rules. Blank lines are now preserved as paragraph separators. |
| `collapse_soft_line_breaks()` | **Only collapses true soft wraps.** Bullets (`•`, `o`, `▪`, `-`), numbered/lettered/roman items, markdown, table rows and headings keep their own line. |
| `is_heading_line()` *(new)* | Detects a heading from a single line: ALL-CAPS, or starts with a known section keyword (`course offering`, `deans update`, `training offering`, `samar college technological institute`, `basic education`, `grading system`, …). |
| `detect_block_header()` *(new)* | Scans the first lines of a block instead of only short one-liners. |
| `smart_chunking()` | Section-aware. A new heading only forces a chunk break when the open chunk is already ≥60 % full, so a heading is never orphaned from the list under it. Chunk bodies keep their line structure so `SCTI → Cookery NC II` stays hierarchical. |
| `is_low_value_chunk()` | Ignores the `[SECTION: ...]` tag when judging chunk value. |
| `normalize_step_grids()` *(new)* | Rebuilds extracted `STEP n` **table grids** into an explicit numbered list with a total count. See "Follow-up: the 10-step grid" below. |


### `store_index.py`

| Item | Change |
|------|--------|
| `merge_continuation_pages()` *(new)* | **The main fix.** Merges adjacent pages of the same source when the next page structurally continues the previous one (both sides are list items, previous page ends mid-sentence, or previous page ends on a label like `Training Offerings:`). Bounded by `MERGE_TOKEN_BUDGET = 900`. |
| `_looks_like_continuation()` *(new)* | The heuristic behind the merge; refuses to merge when a page opens a new `Chapter N` / roman-numeral section. |
| `_approx_tokens()`, `_first_meaningful_line()`, `_NEW_SECTION_START`, `_BULLET_START` *(new)* | Support helpers for the merge decision. |
| `_chunk_and_dedup()` | Runs the merge first, then stitches **bidirectionally** (previous-page tail **and** next-page head) at `STITCH_WORDS = 80` (was a one-directional 40). |
| chunk metadata | Now records `section` (detected heading) and `page_end` (for merged pages). |
| imports | Added `is_heading_line`, `detect_block_header` from `src.helper`. |


### `rag/chain.py`

| Item | Before | After |
|------|--------|-------|
| `EnsembleRetriever` weights | `[0.2 sparse, 0.8 dense]` | `[0.45, 0.55]` |
| per-retriever `top_k` | `10` | `RETRIEVER_TOP_K = 25` |
| `FINAL_TOP_K` | `10` | `12` (after reranking) |
| reranking | none | `rerank()` over `RERANK_CANDIDATES = 40` |
| retrieval call | single `retriever.invoke()` | `multi_query_retrieve()` |
| query expansion | none | `expand_queries()` — generic paraphrases for thin queries (see Follow-up 3) |


`multi_query_retrieve()` runs the original query plus its paraphrases and round-robin merges the
result lists, so a chunk that only the paraphrase surfaces still reaches the reranker.


### `rag/reranker.py` *(new)* — cross-encoder reranking

Hybrid search is a **recall** device; its *ordering* is weak. Example failure seen in the logs for
*"what is the enrollment process for old students?"*:

```
[1] p46  ADMISSION REQUIREMENTS        <- lexically perfect, wrong content
...
     p40  OLD STUDENTS 10-step flow     <- the real answer, ranked too low / cut off
```

BM25 rewards p46 for repeating "admission requirements"; the dense vector rewards the many
`Chapter N … Enrollment Procedure` table-of-contents lines. A cross-encoder reads
`(query, passage)` **jointly**, so the page that actually contains the steps wins.

| Item | Value |
|------|-------|
| model | `cross-encoder/ms-marco-MiniLM-L6-v2` (~90 MB, 6 layers, CPU-friendly) |
| override | `RERANKER_MODEL_NAME`, `RERANKER_MAX_PAIRS` env vars |
| scored input | `metadata['section']` + first 2000 chars of the chunk |
| output | reordered docs, score written to `metadata['rerank_score']` |
| failure mode | **fully non-fatal** — import/download/scoring errors fall back to retrieval order |
| loading | lazy + cached + thread-safe; `warmup()` available for eager load |

`download_model.py` now pre-downloads this model too, so Docker images bake it in instead of
fetching it on the first user question.

### `verify_chunking.py` *(new)*


Offline chunk inspector — no Pinecone, no embeddings, no API calls.

```bash
# Which chunks will contain "SCTI"?
python tools/verify_chunking.py "C:\path\to\Samar-College-update.pdf" --grep SCTI

# Inspect a page range
python tools/verify_chunking.py data/Samar-College-2024.pdf --pages 42-44

# See merge decisions
python tools/verify_chunking.py data/Samar-College-2024.pdf --pages 14-16 --verbose
```

---

## Verified result

Running the new pipeline on `Samar-College-update.pdf` pages 14–16:

```
Produced 1 chunk(s)
CHUNK 1 | p14-16 | section=DEANS UPDATE | 2321 chars  <<< HAS BOTH
```

That single chunk now contains the **entire** offering list:

```
COURSE OFFERING
Degree Programs:
  College of Graduate Studies (CGS) ... College of Education (CoEd) ...
  College of Information Technology and Allied Sciences (CITAS) ...
  College of Business & Management (CBM) ... College of Criminal Justice Education (CoCJE) ...
  College of Liberal Arts and Sciences (CLAS) ...
  Samar College Technological Institute (SCTI) Training Offerings:
    o Cookery NC II
    o Computer System Servicing NC II
    o 3-Years Diploma in Information Technology (DIT)
Basic Education: Senior High School (HUMSS/ABM/ICT/HE), Junior High School, Elementary, Pre-school
DEANS UPDATE ...
```

Base-handbook regression check (`Samar-College-2024.pdf` pp. 36–45) still produces clean,
correctly-tagged sections (`Training Offering`, `BASIC EDUCATION`, `ADMISSION REQUIREMENTS`,
`I. BACKGROUND/RATIONALE`, …) instead of everything being `General College Information`.

---

## Follow-up: the 10-step grid answered with only 6 steps

After the reranker landed, the *right* pages (p46 = New Students, p47 = Old Students) were ranked
first — but the answer still stopped at step 6. This was **not** a retrieval problem. The full
`STEP 1 … STEP 10` text was already inside the retrieved chunk; the problem was its **shape**.

The enrollment procedure is a 5×2 table. PyMuPDF emits table cells in visual order, so the page
extracts as a wall of disconnected fragments:

```
STEP 1
Security & Services
* Log-in personal information
STEP 2
*Secure Assessment slip for back accounts
STEP 3
Enrolling Table Per Program *Present Clearance ...
```

Nothing in that text says *"this is one procedure and it has ten steps"*. Each cell reads like an
independent snippet, so the model summarized the first few and stopped — plausibly, and wrongly.

Two concrete defects, both now fixed in `src/helper.py`:

**1. `normalize_step_grids()` — make the sequence explicit.**
`clean_text()` now rewrites any run of `STEP n` grid cells into a self-describing list:

```
[STEP SEQUENCE]
STEP 1: Security & Services -- Log-in personal information; Secure queue number
STEP 2: Secure Assessment slip for back accounts *Secure Clearance Form *Secure ID Information Form
...
STEP 10: Announcement -- Attend Regular Classes
[END STEP SEQUENCE: 10 steps total]
```

The trailing count is the important part: an incomplete answer becomes self-evidently wrong.
A run is only broken when the step number **stops increasing**, which is exactly the boundary
between the New-Students grid (1…10) and the Old-Students grid (1…10) that follows it on the next
page — so the two procedures no longer bleed into each other.

Only uppercase `STEP n` **alone on its line** counts as a grid cell. Prose like
`Step 1: Submit Credentials Step 2: Follow the Enrollment Process` is left untouched.

**2. `[SECTION: STEP 10]` — a table cell was being used as the section title.**
`is_heading_line()` treats ALL-CAPS lines as headings, and `STEP 10` is ALL-CAPS. The running
header therefore latched onto `STEP 10` and every chunk on those pages was tagged
`[SECTION: STEP 10]` — useless for retrieval, and actively misleading to the reranker, which scores
`metadata['section'] + chunk`. A `_NOT_A_HEADING` guard now rejects `STEP n`, `TABLE n`, `FIGURE n`
and the sequence markers.

Verified on `Samar-College-2024.pdf` pp. 46–47:

| | before | after |
|---|---|---|
| p46 section | `STEP 10` | `ENROLLMENT PROCEDURE` |
| p47 section | `STEP 10` | `STEPS IN SIGNING OF CLEARANCE` |
| steps rendered | 10 fragments, no total | `STEP 1…10` + `[END STEP SEQUENCE: 10 steps total]` |

Regression-checked against prose pages (pp. 14–16 history) and the SCTI page (p21): output
unchanged, so the normalizer is inert on non-grid content.

---

## Follow-up 2: the same grid, but as Markdown

The offline check above passed while the **deployed** assistant still truncated the answer. Probing
the live index with `probe_retrieval.py` explained why: the retrieved chunk contained the grid as a
**Markdown table**, not as one-cell-per-line text.

```
| **STEP 1**<br>**Security & Services**<br>* Log-in personal information | **STEP 2**<br>... |
```

There are two extraction paths in `store_index.py` and they produce different shapes:

| Path | Output shape | Handled by the first fix? |
|------|--------------|---------------------------|
| `extract_columns_text()` / OCR | `STEP 1` alone on a line | yes |
| `_extract_and_append_tables()` → Vision | one pipe-row, `<br>` inside cells | **no** |

`normalize_step_grids()` is line-based, so the entire 10-cell grid was a *single line* and no grid
was ever detected. Three additions close the gap:

| Function | Purpose |
|----------|---------|
| `_flatten_markdown_step_tables()` *(new)* | Explodes a Markdown row into one cell per line, converting `**STEP 1**` → `STEP 1`, so the existing detector sees a normal grid. **Only rows containing `STEP n` are touched** — grading breakdowns and curriculum maps pass through byte-for-byte. |
| `split_inline_bold_steps()` *(new)* | Puts each `**Step n:**` label on its own line. The clearance list arrived as one run: `**STEPS IN SIGNING OF CLEARANCE** **Step 1:** Registrar **Step 2:** …`. Markdown table rows are skipped so the flattener still sees intact rows. |
| `_MD_BOLD_HEADING` / `_MD_BOLD_STEP` guards | `collapse_soft_line_breaks()` now treats `**BOLD HEADING**` and `**Step n:**` as block starts, so a Vision-emitted heading is no longer glued to the following sentence. |

`normalize_step_grids()` also stops the **last** cell from swallowing the heading that follows the
table: the body is cut at the first heading line, and the remainder is re-emitted *after*
`[END STEP SEQUENCE]`.

Result on the exact text pulled from the live index:

| | before | after |
|---|---|---|
| grid detected | no (one Markdown line) | yes |
| steps rendered | 0 | `STEP 1…10` + `[END STEP SEQUENCE: 10 steps total]` |
| last step | `Announcement -- Attend Regular Classes; STEPS IN SIGNING OF CLEARANCE** **Step 1:** …` | `Announcement -- Attend Regular Classes` |
| clearance list | one paragraph | `Step 1:` … `Step 8:`, one per line |

---

## Follow-up 3: SCTI *still* missing — four measured causes

The merge fix above put SCTI in the same chunk as the rest of the list, yet the deployed
assistant still answered without the SCTI training offerings. Probing the **live index** showed
the text was indexed correctly, so this was purely a **ranking** failure. Four separate defects,
each measured with the real cross-encoder:

### 1. The reranker only read the first 2000 characters

`rerank()` scored `section + chunk[:2000]`. The program list is ~2400 characters and SCTI sits at
the **end**, so the deciding evidence was never shown to the model.

| scored window | score for *"what are the programs offered?"* |
|---|---|
| first 2000 chars (old) | **-9.80** |
| window containing SCTI | **+8.70** |

Fixed by scoring **overlapping 1800-char windows** (400-char stride) and keeping the **maximum**.
Long chunks are no longer penalised for having their answer late in the text.

### 2. Thin queries carried too little signal

A 2-word query gives a cross-encoder almost nothing to match on:

| query | score of the program chunk |
|---|---|
| `programs offered` | **0.17** |
| `list of all degree programs, majors and training offerings` | **8.81** |

`expand_queries()` now detects a **thin** query (≤4 content words) and adds a generic paraphrase
built from the query's own words — `"list of all <terms>"`, `"complete list of <terms> offered"`.
The previous version hard-coded `SCTI Cookery NC II DIT …` into the prompt, which only ever fixed
this one question; the new rule is vocabulary-free and helps every short query.

### 3. The chunk was labelled with the WRONG section

`smart_chunking()` kept a single `running_header`, overwrote it at every heading, and stamped the
**last** value onto the chunk at emit time. A page containing `COURSE OFFERING … DEANS UPDATE`
therefore filed the entire program list under `DEANS UPDATE`. Since the label is prepended to
every reranker window, that is a self-inflicted penalty:

| label on the program chunk | score |
|---|---|
| `DEANS UPDATE` (what the old code produced) | **-6.51** |
| no label at all | -5.64 |
| `COURSE OFFERING` (correct) | **+0.15** |

Fixed two ways: `emit_current()` uses the heading in force when the chunk **started**, and a new
**major** (all-caps) heading always closes the open chunk so two sections can never share one.

### 4. OCR pages have no blank lines, so mid-page headings were invisible

Blocks are split on blank lines and only the first 3 lines of a block are examined for a heading.
Vision/OCR output contains almost no blank lines, so a whole page arrives as **one block** whose
first line is arbitrary. On `Samar-College-update.pdf` p12 the indexed chunk was:

```
[SECTION: X.  SUGGESTED REFERENCES]      <- the program list was buried inside
... grading table ... SUGGESTED REFERENCES ... HOUSE RULES ... CONSULTATION HOURS ...
COURSE OFFERING ... Samar College Technological Institute (SCTI) ... Cookery NC II ...
```

`split_blocks_on_major_headings()` (new, in `src/helper.py`) re-splits every block so each major
heading starts its own block. The existing chunking rules then apply to OCR text exactly as they
do to text-layer PDFs.

| | before | after |
|---|---|---|
| p12 chunk label | `X. SUGGESTED REFERENCES` | `COURSE OFFERING` |
| chunks from update.pdf | 232 vectors total | 303 vectors total |

### End-to-end result

Real pipeline (retrieval → rerank → LLM), no hardcoded vocabulary anywhere:

| question | top hit | SCTI in answer |
|---|---|---|
| `what are the programs offered?` | `COURSE OFFERING` | ✅ |
| `programs offered` | **update.pdf** `COURSE OFFERING` (5.63) ahead of handbook (5.20) | ✅ |
| `What courses can I take at Samar College?` | **update.pdf** `COURSE OFFERING` (6.92) | ✅ |

The newer document now outranks the old handbook on the same question, which is the desired
behaviour for an "update" upload.

---


## ⚠️ You must re-index

Chunk text and chunk IDs changed, so the vectors currently in Pinecone are stale.


**Option A — re-upload just the update PDF (fastest)**

1. Delete the existing `Samar-College-update.pdf` vectors from the index.
2. Re-upload the file through the admin page. `append_file_to_index()` will use the new pipeline.

**Option B — full rebuild (recommended, also refreshes BM25)**

```bash
# put BOTH PDFs in data/ first, otherwise the update PDF is dropped
copy "C:\Users\Rojan\Downloads\Samar-College-update.pdf" data\

python store_index.py
```

A full rebuild also regenerates `bm25_values.json` from the complete corpus. The current file was
fitted on only **146 docs**, and `append_file_to_index()` merges new stats into it incrementally —
workable, but a clean refit gives the best IDF weighting for new vocabulary such as `SCTI`,
`BMMA`, and `CITAS`.
