# Data Conflicts — why the AI "gets confused", and how this fixes it

## The actual bug

`data/Samar-College-update.pdf` names the Dean of the College of Education **twice, with two
different people**:

| Page | Text in the document | Person |
|------|----------------------|--------|
| p.15 | `Jacqueline Montalis – College of Education` (under **DEANS UPDATE**) | Jacqueline Montalis |
| p.26 | `Dr. Nimfa T. Torremoro / Dean, College of Graduate Studies & College of Education` | Nimfa T. Torremoro |

Both lines are indexed. Both are retrieved for *"who is the dean of CoEd?"*. Both are equally
"grounded in the documents". Nothing in the text says which one is current, so the model picks
whichever chunk ranked higher that run — and the answer changes between runs.

**This is not a prompt problem or a retrieval problem.** No instruction can pick the right one,
because the information needed to decide is not in the corpus. The contradiction must be
**detected**, reviewed **once by a human**, and then **enforced** at answer time.

## The three pieces

```
1. DETECT     rag/conflicts.py        extract (role, subject) -> person facts, group them,
                                      report any key with more than one person
2. REVIEW     Dashboard → Data        admin sees both values + the exact page each came from,
              Conflicts               and picks the correct one (or types a third value)
3. ENFORCE    rag/chain.py            the decision is injected as <verified_facts> ahead of the
              src/prompt.py           retrieved docs, with the rejected value named explicitly
```

### Why key/value extraction, not "duplicate detection"

Near-duplicate/embedding similarity finds text that *looks alike*. The failure here is the
opposite: two short, lexically **different** lines assigning the same **role** to different
**people**. Only a `(role, subject) → person` view makes that visible — and it gives the admin
something concrete to choose between instead of two walls of text.

Extraction is structural, not hardcoded. Three shapes are matched:

| Shape | Example | Where it occurs |
|-------|---------|-----------------|
| roster line under a role heading | `DEANS UPDATE` … `Jacqueline Montalis – College of Education` | update.pdf p.15 |
| name line + `ROLE, SUBJECT` line | `Dr. Nimfa T. Torremoro` / `Dean, College of Graduate Studies & College of Education` | update.pdf pp.16–31, ANNEX C |
| prose | `The dean of CITAS is Juan Dela Cruz` | anywhere |

No Samar-College vocabulary appears in the patterns, so a newly uploaded document is checked the
same way on day one.

**Noise control** (an admin who sees fake conflicts stops trusting the tool):
- honorifics and post-nominals are stripped — `Dr. Nimfa T. Torremoro, PhD` ≡ `NIMFA T. TORREMORO`
- middle initials are dropped — `Nimfa T. Torremoro` ≡ `Nimfa Torremoro`
- hyphens/apostrophes are flattened — `Mary-Ann D. Abaigar` ≡ `Mary Ann Abaigar`
- `Surname, Firstname` is flipped to `Firstname Surname`
- `Dean, College of Graduate Studies & College of Education` is recorded against **both**
  colleges, because one person legitimately holding two deanships is not a conflict — but it must
  still be counted for each college, or the real conflict on one of them stays hidden
- role synonyms collapse (`Deans`, `Acting Dean`, `College Dean` → `dean`), otherwise a conflict
  quietly splits into two harmless-looking single-value keys

## Using it

### CLI (fast, no server needed)

```bash
python tools/check_data_conflicts.py                  # scan data/
python tools/check_data_conflicts.py --s3             # scan the live S3 corpus (what the AI sees)
python tools/check_data_conflicts.py --all-facts      # dump every extracted fact
python tools/check_data_conflicts.py --json           # machine-readable
```

Exit code is `1` while unresolved conflicts remain, so it can gate a re-index:

```bash
python tools/check_data_conflicts.py && python store_index.py
```

Current output for this repo:

```
Facts extracted : 21
Conflicting keys: 1

[NEEDS REVIEW] Dean of College Education
    * Jacqueline Montalis      [1x]  Samar-College-update.pdf p15
        "Jacqueline Montalis – College of Education"
    * Dr. Nimfa T. Torremoro   [1x]  Samar-College-update.pdf p26
        "Dr. Nimfa T. Torremoro / Dean, College of Graduate Studies & College of Education"
```

### Admin UI

**Dashboard → Data Conflicts** (sidebar; the badge shows the unresolved count).

Each card shows the question students are effectively asking, every value the documents give, the
**file + page** each value came from, how many times it is stated, and the exact sentence. The
admin then either:

- selects the correct value, **or**
- types a different value — because the document can be wrong in *both* places, and forcing a
  choice between two wrong names would make the tool useless

Optional note (e.g. *"Confirmed by Registrar, AY 2025-2026"*) is stored with the decision, and
**Undo** re-opens the item.

Endpoints: `POST /admin/conflicts/api/scan`, `/api/resolve`, `/api/unresolve`,
`GET /admin/conflicts/api/resolutions`. All admin-gated.

### What the AI receives afterwards

Decisions live in `conflict_resolutions.json` (override with `CONFLICT_RESOLUTIONS_FILE`).
On every question, `authority_block()` matches them against **both** the user's question **and**
the retrieved text, then prepends:

```
<verified_facts>
These values were reviewed and confirmed by a college administrator.
They OVERRIDE any conflicting statement in the retrieved documents.
...
- Dean of College Education: Jacqueline Montalis
  (outdated / incorrect, do NOT use: Dr. Nimfa T. Torremoro)
  note: Confirmed by Registrar, AY 2025-2026
</verified_facts>
```

Two details that matter:

1. **The rejected name is stated explicitly.** Saying only "X is correct" leaves the model free to
   repeat the stale name it can still see in the context. Naming it as outdated closes that path.
2. **Matching includes the retrieved text, not just the question.** If a wrong name appears in the
   context for *any* question, the correction travels with it.

Nothing is injected when no decision applies, so the prompt is unchanged for the vast majority of
questions.

## Answering the design question: is a UI the right call?

Alternatives considered:

| Option | Verdict |
|--------|---------|
| Newest file wins | Both conflicting lines are in the **same** file (p.15 vs p.26). Recency cannot break the tie. |
| Ask the LLM to decide | It would be guessing from the same contradictory text — the current bug, with extra latency. |
| Edit the PDF and re-index | The correct long-term fix, but it needs the document owner, and the assistant is wrong in the meantime. |
| Let the AI answer "there are two conflicting records" | Honest, but useless to a student asking a factual question — and it exposes an internal data problem as a student-facing answer. |
| **Admin picks once; system enforces** | One human decision, applied instantly, no re-index, and it keeps working after the PDF is eventually fixed. |

Recommended workflow: **resolve in the UI now** (immediate correctness) and **fix the source
document when convenient**. After the PDF is corrected and re-indexed, the conflict disappears
from the scan and the resolution becomes a harmless no-op — safe to leave or remove with *Undo*.

## Verifying

```bash
python tools/check_data_conflicts.py     # detection, against the real PDFs
python tests/test_conflicts_api.py       # the admin endpoints + the prompt override
python run_tests.py                # everything, both features
```

`test_conflicts_api.py` checks the parts that are easy to get silently wrong: only admins can pin a
value (a student deciding "who the dean is" would be worse than the original bug), a decision
survives the round-trip with its rejected value and note, and — the one that matters most — the saved
decision actually shows up inside `authority_block()`, naming the correct value *and* the wrong one to
ignore. An endpoint that returns `200` but never reaches the prompt would look perfect in the UI while
changing nothing about the AI's answer, so the test follows the decision all the way into the prompt
text and asserts an unrelated question gets no injection at all.

Storage is redirected to a temp file, so `conflict_resolutions.json` is never touched and no AWS
credentials are needed.

## Extending

Add a role to `_ROLE_CANON` in `rag/conflicts.py` and it is detected everywhere — no other change.

To share decisions across multiple app instances, swap `_load_store` / `_save_store` for DynamoDB;
the rest of the module is unaffected.
