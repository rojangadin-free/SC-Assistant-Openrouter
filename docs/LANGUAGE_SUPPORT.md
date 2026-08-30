# Taglish & Waray Support

## The problem

Every document in the knowledge base is written in English. Almost no student in
Samar asks a question that way:

| What the student types | Language |
| --- | --- |
| `Pila an bayad sa enrollment?` | Waray-Waray |
| `Magkano ang tuition fee?` | Tagalog |
| `Sin-o an dean han education?` | Waray |
| `Kailan ang deadline ng payment?` | Taglish |

Retrieval is BM25 + embeddings over English text, and both halves fail on that
input for different reasons:

* **BM25 scores it exactly zero.** `pila`, `bayad` and `sin-o` do not occur in
  any indexed document, so there is no term to match. Not "a weak match" — no
  match.
* **The embedding is English-only.** `all-MiniLM-L6-v2` was trained on English;
  `pila an bayad` does not land anywhere near `tuition fees` in its vector space.

The result is not a wrong answer, which would at least be visible. It is
`"I don't have information about that"` for a question the handbook answers on
page 20 — delivered to the students least likely to switch to English and try
again.

## The fix

`rag/language.py` adds an **English retrieval variant** for local-language
questions, using a lookup table of the ~180 words that actually appear in campus
questions.

```
"Pila an bayad sa enrollment?"
  -> queries: [
       "Pila an bayad sa enrollment?",              # the student's own words
       "how much is payment fee enrollment",        # the English variant
       "how much is payment fee enrollment list of fees and amounts",
     ]
```

All three are retrieved and reranked together. The student's own wording is
always query #1, so this can only add recall, never remove it.

### Why a table instead of an LLM translation

| | Dictionary | LLM translate-then-search |
| --- | --- | --- |
| Latency | 0 ms | +1 round-trip **before** retrieval starts |
| Cost | none | one extra call per question |
| Determinism | exact, testable | varies run to run |
| `pila` | `how much` (Waray) | often `several` (the Tagalog sense) |
| Maintainable by staff | yes — it is a list of words | no |

The latency point is the decisive one: the extra round-trip would be paid by the
exact population that already has the worst experience.

`pila` is also the clearest example of why translation quality matters more than
translation coverage. In Tagalog it means "several"; in Waray it means "how
much", and it opens most Waray fee questions. A general-purpose translator gets
this wrong in the direction that destroys the query.

## What it deliberately does not do

* **It does not translate sentences.** Word order and grammar are irrelevant to
  BM25 and near-irrelevant to a bag-of-subwords embedding, so no effort is spent
  on them. `"how much is payment fee enrollment"` is not English prose; it is a
  search string.
* **It does not touch the answer's language.** `src/prompt.py` instructs the
  model to reply in whatever language the student used. The rewrite is a search
  mechanism, invisible to the student.
* **It does not replace the original query.** A word the table gets wrong costs
  one candidate slot out of 40. It can never cost the question the student asked.

## Rules for editing the table

**Only add a key that is NOT an English word the documents contain.**

An early draft included identity entries — `"tuition": "tuition"`,
`"dean": "dean"`, `"requirements": "requirements"`. They look harmless. They are
not: a plain English question containing any of them (`"Who is the dean?"`)
counted as a local-language hit and fired the whole rewrite path, buying an extra
Pinecone round-trip and a duplicate ranking slot in exchange for nothing.
`test_language.py` now asserts that English questions produce no variants at all.

Multi-word keys are matched before their own components, so `pila an` becomes
`how much is` rather than `how much` + a separately translated `an`.

A value may contain several English words (`"bayad": "payment fee"`) — this is
how one local word covers the several English terms the documents use for it.

## Files

| File | Role |
| --- | --- |
| `rag/language.py` | the term table and the rewrite |
| `rag/chain.py` | calls `language_variants()` inside `expand_queries()` |
| `src/prompt.py` | "answer in the language the student used" |
| `test_language.py` | 52 assertions, no network or credentials |

## Tests

```
python tests/test_language.py      # this layer only
python run_tests.py          # the whole data-quality suite
```

The suite covers detection (including that English is left alone), the term
mappings, particle removal (`po`, `ba`, `sir`), de-duplication, the additive
ordering guarantee inside `expand_queries()`, and ten real student questions
asserted against the one English keyword each needs to hit the right page.
