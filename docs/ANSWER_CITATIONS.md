# Answer Citations — "Based on" source footer

## The problem

The retrieval pipeline already knows, precisely, which file and page every answer
was built from. `docs_to_context()` stamps `Source:` and `Page:` onto each chunk,
and the system prompt forbids answering from anything else.

The student never sees any of it.

That has three costs:

1. **Nothing is checkable.** A correct answer and a confidently wrong one look
   identical on screen. For fees, deadlines and requirements — where being wrong
   has consequences — that is the difference between a tool and a rumour.
2. **Every answer is a dead end.** A student who wants the surrounding rules has
   to go find the handbook themselves, which is the task they came here to avoid.
3. **Wrong answers are unactionable.** Answer Quality (`rag/feedback.py`) groups
   downvotes by *suspect document*, and Data Conflicts (`rag/conflicts.py`) needs
   to know which file carried a stale value. Both are far stronger when the
   student can see — and report — the file that produced the claim.

The model *can* emit inline `[SOURCE: file | page]` markers, and `chat.js` already
renders those as superscript badges. But that is the model's choice, mid-sentence,
and it is not reliable: some answers carry no marker at all. The footer is derived
from the retrieval result, so it does not depend on the LLM cooperating.

## What was added

A quiet line under each answer:

> 📖 Based on   `Samar-College-update.pdf, pp. 14-15`   `Samar-College-2024.pdf, p. 3`

Each chip is a link that opens the file at the cited page.

### Files

| File | Role |
|---|---|
| `rag/citations.py` | Groups the documents sent to the LLM into one entry per file, collapsing page runs. Pure data shaping — no I/O. |
| `rag/chain.py` | Calls `build_citations(final_docs)` and returns it in the graph state as `citations`. |
| `sc_assistant/chat.py` | Passes `citations` through the SSE `done` event — suppressed when the answer was a refusal. |
| `sc_assistant/static/js/chat.js` | `renderSourceFooter()` draws the chips; `sourcesFor()` now also reads them, so votes carry provenance. |
| `sc_assistant/templates/chat.html` | `.answer-sources` / `.source-chip` styling. |
| `test_citations.py` | 18 offline checks. Wired into `run_tests.py`. |

## Design decisions

**Cite what was sent, not what was retrieved.** The retriever pulls ~25 candidates
per query and the cross-encoder cuts that to 12. Citing the candidate pool would
be noise pretending to be rigour: those pages did not inform the answer.

**Group by file, collapse pages.** Twelve chunks are usually four pages of one
PDF. `pp. 14-16, 20` is one honest line; twelve near-identical lines teach the
student to ignore the block entirely — at which point the feature has negative
value, because it occupies space and earns nothing.

**Keep the reranker's order.** The first chip is the file that produced the
highest-scoring chunk. Sorting alphabetically would put the page that mattered
wherever the alphabet happened to place it.

**Cap at four files.** Past that, a citation stops being a pointer and becomes a
bibliography. The cap is on files, not chunks, so a genuinely well-sourced answer
is never reduced to a single page.

**Suppress on a refusal.** If the assistant said "I don't have that information",
listing the pages it read implies those pages answered the question. The check
reuses `looks_unanswered()` — the same function that drives Content Gaps — so the
two features cannot drift apart: whatever counts as a refusal for logging counts
as a refusal for citations.

**Send with `done`, not with the chunks.** The footer is a property of the finished
answer. Re-rendering it on every token would make it flicker, and would print
sources under a sentence that has not finished making its claim.

**Return plain data, not HTML.** `build_citations()` yields dicts, so the same
payload serves the SSE stream, the feedback vote, and anything added later
(stored conversations, an admin view) without a parser.

## Interaction with existing features

- **Answer Quality** — `sourcesFor()` reads the footer chips in addition to inline
  badges, so a 👎 now always travels with the documents it is judging. Previously a
  vote on an answer with no inline markers arrived with an empty `sources` list,
  which is exactly the case where the admin most needs to know where to look.
- **Data Conflicts** — when a student sees two different names for the same role,
  the footer names the two files. That is the report the admin needs in order to
  resolve the conflict.
- **Content Gaps** — unaffected by construction: no citations are shown precisely
  when a gap is logged.

## Verification

```
python tests/test_citations.py     # 18/18
python run_tests.py          # 5/5 suites
```

The unit suite covers S3-key and Windows-path basenames, page-range collapsing,
per-file grouping with duplicate pages, rank preservation, the file cap, and the
degenerate cases (no docs, `None`, missing source, missing page, `"12.0"` page
strings) — because citation code runs on every answer and must never be the thing
that raises.
