# Misspelled questions (`rag/spelling.py`)

> "latin honir requirements" must find the Latin honors page.

## The bug this fixes

A student typed **`latin honir requirements`** and got a vague, wrong-ish
answer. Nothing in the pipeline was broken — every layer did exactly what it was
designed to do:

| Layer | What it did | Why that was reasonable |
|---|---|---|
| `rag/latency.py` → `should_optimize()` | **Skipped the LLM query rewrite.** | Three content words (`latin`, `honir`, `requirements`), no pronouns, no follow-up marker. By every rule in that file the question is self-contained, and skipping saves a whole round-trip. |
| BM25 (sparse retrieval) | **Scored `honir` at zero.** | The token appears in no chunk of the handbook. BM25 cannot score what it has never seen. |
| Dense retrieval | **Embedded a nonsense token.** | The MiniLM encoder has no vector for `honir`; it blurs the query away from "GRADUATION HONORS AND AWARDS". |
| `rag/gaps.py` | **Filed the question as a missing document.** | The answer was a refusal-shaped one, so the content-gap log did its job. |

Net effect: the strongest search term in the question contributed **nothing**,
the query silently degraded to `latin … requirements` — which fits admission
requirements, transfer requirements and graduation requirements equally badly —
and the admin's "documents to add" list got polluted with a page that already
exists.

One transposed vowel. No error anywhere.

## The second bug: a real word that is the wrong word

Reported right after the first fix shipped:

```
typed:     "lost the dens"
got:       "lost the deans"     <- only half fixed
meant:     "list the deans"
```

`dens` was repaired because no document contains it. `lost` was **not**, and
for a good reason: it is a perfectly ordinary English word, so `is_known()`
answered True and the corrector never looked at it. Correct in isolation,
wrong in this sentence — and a spell checker literally cannot see the
difference. Only the neighbouring words can.

So a *known* word is now reconsidered, but only under two pieces of evidence:

1. it is in an explicit confusion table (`lost`→`list`, `gice`→`give`,
   `mich`→`much`, `want`→`what`, `wgo`→`who`, `whne`→`when`, …) — a table, not
   a distance search, because opening real words to edit distance would let
   "dean" become "dear", "mean" or "dead";
2. the sentence supplies the context the replacement needs. `list` needs a
   determiner after it **and** a plural noun somewhere in the sentence.

Which is exactly what separates these:

| Typed | Repaired to | Why |
|---|---|---|
| "lost the dens" | "list the deans" | `the` follows, `deans` is plural |
| "lost all programs offered" | "list all programs offered" | same shape |
| "i lost my id" | *(unchanged)* | `my` is not an expected follower, no plural |
| "lost and found office" | *(unchanged)* | `and` is not an expected follower |
| "the lost item procedure" | *(unchanged)* | no plural noun |
| "i lost the documents" | *(unchanged)* | `lost` is inside a statement, not at the request opening |
| "lost all my documents" | *(unchanged)* | a possessive identifies a personal loss |
| "i want the list of deans" | *(unchanged)* | `want` is inside a statement |

Two more guards came out of sweeping real questions against this:

* **Inflections are not typos.** "item" → "items" is a grammar edit; BM25
  already matches the two forms, so the probe would be a duplicate *and* would
  wrongly tell `rag/latency.py` the question was unsearchable.
* **Short words may not swap a consonant.** "talk" → "task" and Waray "para" →
  "part" were both live false positives. Below six letters a repair must either
  *add* a letter ("dens" → "deans") or swap a **vowel for a vowel** ("honir" →
  "honor") — a word's consonants are what identify it.

## The third bug: the repair did not control the answer path

The local checker already repaired `lost the dens` to `list the deans`, but chat
still used the original text as the primary retrieval and ranking query, the
optimizer input, and the final question sent to the answer model. Adding one
corrected probe was not harmless: results from the typo could occupy the limited
answer context and displace the evidence retrieved for the intended question.
This happened with reranking disabled and could also happen when it was enabled.

## The fix

### 1. One repaired working question throughout chat (no network)

`rag/chain.py` applies `correct_query()` to a local copy of the input before any
preparation tasks start. That same working question is used for the optimizer
decision and prompt, retrieval, reranking, and the text or image answer prompt.
The original `state["input"]` and saved/displayed conversation are not modified.

```python
typed = "lost the dens"
working = "list the deans"
queries = [
    "list the deans",
    "complete list of deans",
    "deans include the following",
]
```

The `spelling_variants()` helper remains additive for direct callers. In chat,
accepted repairs define the working question instead of competing with the typo.
Ambiguous repairs are declined, and real-word confusions require request-position
and object evidence so genuine loss or preference statements stay unchanged.

### 2. The optimizer decision uses that same working question

`should_optimize()` gained one rule, checked before all the others:

```python
if has_misspelling(q):
    return True
```

Counting content words measures how *much* a question carries. This measures
whether those words **exist in the corpus at all** — a different question, and
the one that decides whether a working question is searchable. Chat repairs it
first, so an accepted correction takes the same optimizer skip/wait decision as
correctly spelled input. Thin questions and conversational follow-ups still get
the bounded rewrite; a self-contained repaired question needs no extra LLM call.
The optimizer retains its spelling rule and hints for remaining repairable typos.

## Why a dictionary of the corpus, not of English

A general spell checker is wrong twice over here. It does not know `BSIT`,
`CITAS`, `SCTI`, `Waray` or `laude` — so it would "fix" the exact terms that
matter most — and it does not know which of its suggestions exists in *these*
documents, which are the only suggestions worth making.

So the vocabulary **is** the corpus: every word the indexed documents contain,
with how often it appears, in `spell_vocab.json`.

`bm25_values.json` cannot supply this. `pinecone-text` stores hashed token ids
and document frequencies, not the tokens themselves, so the corpus vocabulary is
unrecoverable from the index once it is built. It is therefore collected from the
chunk text at index time.

## Safety rules

Every one of these exists to stop a working question from being "corrected":

| Rule | Why |
|---|---|
| Words shorter than 4 characters are never touched | `cum` is one edit from `sum`, `cut`, `cup`. `shs`, `jhs` likewise. |
| Under 6 characters: insertions, or vowel-for-vowel only | `talk`→`task` and `para`→`part` are one substitution away and destroy the query. `dens`→`deans` (insertion) and `honir`→`honor` (vowel slip) are safe. |
| A real word is only replaced with request evidence | It must open the request, optionally after `please`/`kindly`. `lost` also needs a non-personal plural object; loss and preference statements stay unchanged. |
| Inflections are not corrections | `item`→`items` is grammar, not spelling; BM25 already matches both. |
| The first letter must match | Typos almost never change the first character; requiring it kills most false matches. |
| Edit distance ≤ 1 (≤ 2 for words of 7+ characters) | Transpositions count as **one** edit — `honir`, `requirments`, `enrolmnet` are all one slip of two fingers. |
| A target must appear 3+ times in the corpus | OCR invents words (`rcgistrar`, `thc`). A word garbled once is not a spelling suggestion. |
| Ambiguous repairs are declined | Two corpus words equally close and equally common mean no correction; the original question remains the working question. |
| At most 3 repairs per message | Five unknown words is not a typo — it is Waray (`rag/language.py`), or a paste. |
| Ordinary English glue is "known" | `please`, `about`, `there` are never corrected and never offered as corrections. |
| Acronyms and dictated letter runs are left alone | `rag/dictation.py` owns `b s i t` → `bsit`; a spell checker guessing at it would fight that. |
| Local-language words are left alone | `bayad`, `pila`, `magkano` are not typos — `rag/language.py` translates them, and pulling them toward an English word would destroy the signal. |

## Keeping the vocabulary current

It is refreshed automatically by **indexing** — `store_index.py` calls
`learn_terms()` on the chunk text right after BM25 is trained, on both the full
build and the admin's single-file upload. So a newly uploaded handbook's
vocabulary is learned at the same moment its pages become searchable.

To rebuild it by hand from `data/` (e.g. after a fresh checkout):

```bash
python tools/build_spell_vocab.py
```

To ask what would happen to a specific question:

```bash
python tools/build_spell_vocab.py --check "latin honir requirements"
# 'latin honir requirements' -> 'honir' looks like 'honor'
```

`spell_vocab.json` is git-ignored for the same reason as `doc_freshness.json`: it
describes the corpus **this** deployment indexed, and it is cheap to rebuild.

If it is missing entirely, `rag/spelling.py` falls back to a baked-in seed list
of campus terms — so the headline repair still works on a fresh checkout, just
with a smaller vocabulary.

## What it costs

* **A correctly spelled question: nothing.** No extra probe, no extra
  round-trip, no change to `should_optimize()`. This is the overwhelming
  majority of messages.
* **An accepted spelling repair:** local string work, followed by the same probes
  and optimizer decision as its correctly spelled counterpart. No extra network
  call is required just because a repair was applied.

## Tests

```bash
python tests/test_spelling.py
```

No network or model weights are required. The suite writes its own temp vocabulary
and executes the actual chat graph and retrieval functions with fake services.
It checks repair and safety rules, optimizer decisions, and source wiring. The
`lost the dens` regression compares optimizer input, retrieval probes, ranking
queries, selected context/citations, and the final question against `list the
deans`, with reranking both off and on. It also checks image prompts and preservation
of the original input/history.


## Files

| File | Role |
|---|---|
| `rag/spelling.py` | The repair, the vocabulary, the thresholds. |
| `tools/build_spell_vocab.py` | Build/inspect `spell_vocab.json` from `data/`. |
| `rag/chain.py` | Repairs the working question before every chat stage; retains additive expansion helpers. |
| `rag/latency.py` | A typo means "not searchable as typed" ⇒ wait for the rewrite. |
| `store_index.py` | Learns the vocabulary as documents are indexed. |
| `tests/test_spelling.py` | The suite above. |
