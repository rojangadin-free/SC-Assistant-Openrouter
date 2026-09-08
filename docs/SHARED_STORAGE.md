# Shared storage for admin state (`rag/store.py`)

## The bug

Eight modules keep admin state in JSON files beside the app:

| store | file | what is lost if it is not shared |
|---|---|---|
| `conflicts` | `conflict_resolutions.json` | the pinned correct value is not **enforced** |
| `gaps` | `content_gaps.json` | "asked 14 times" reads as "asked 5 times" |
| `feedback` | `answer_feedback.json` | votes split into two too-small samples |
| `escalations` | `escalations.json` | a student's question is never seen by the admin |
| `announcements` | `announcements.json` | a posted notice is missing for half the traffic |
| `calendar` | `calendar_periods.json` | enrollment dates revert to "not set" |
| `freshness` | `doc_freshness.json` | the superseded handbook wins again |
| `index_jobs` | `index_jobs.json` | an upload's progress bar has no job to report |


That is the correct choice for one process. `Dockerfile` does not describe one
process. Two containers behind a load balancer each get their own writable layer:

```
admin  → instance A : "Dean of College of Education = Jacqueline Montalis"
student → instance B : "who is the dean of CoEd?"
         instance B never saw the decision → answers "Dr. Nimfa T. Torremoro"
```

Nothing throws. Nothing is logged. The feature is simply off for part of the
traffic, and whoever tested it saw it work because their session pinned them to
one instance. That is the failure mode worth spending code on.

## The fix

One seam, `rag/store.py`, holding a `JsonBlobStore` per store. Each module kept
its own dict shape and its own file path and now just asks the seam to load/save:

```python
def _load() -> dict:  return _store().load()
def _save(store) -> None: _store().save(store)
```

Switched by environment variable, defaulted in `config.py`:

```bash
STORE_BACKEND=dynamodb   # default — one item per store, shared by every instance
STORE_BACKEND=file       # opt out: local JSON files, single process only
STORE_TABLE_NAME=SCAssistantStores   # optional
```


### Turning it on

```bash
python tools/create_stores_table.py     # creates the table AND copies the local JSON in
```


The migration step matters: without it the switch looks like data loss, because
the app would start reading a table nobody has written to yet.

Two commands confirm it worked:

```bash
python tools/probe_stores.py            # which stores are in the table vs on disk
python tools/verify_settings_persist.py # writes a period, reads it from a fresh process
```

### The half-finished version of this fix

`STORE_BACKEND=dynamodb` was set and four stores still reset on every deploy. The
migration in `create_stores_table.py` only listed the original four; `announcements`,
`calendar`, `freshness` and `index_jobs` were added later and never added here, so
they had no row in the table. On a fresh container their first read returned empty
and their first write created the row — which is why "the announcement I posted
yesterday is gone" and "my calendar dates are back to unset" looked like two
unrelated bugs.

Nothing detected this, because a missing entry in a migration dict is not an error.
Both guards added here exist to make the same omission loud next time:

- `_warn_about_unmigrated_stores()` greps `rag/` for every `JsonBlobStore("name")`
  and prints a warning for any the migration does not cover.
- `verify_settings_persist.py` hides the local JSON files and reads back from a new
  interpreter, which is the only way to reproduce a redeploy on one machine. Without
  the hiding step it passes on the file backend too — the false negative that let
  this ship.

One more trap, worth naming because it hid the fix rather than caused it: the
"table already exists" branch printed an `ℹ️` to a cp1252 Windows console and died
with `UnicodeEncodeError` *while reporting success*, taking the migration below it
down. `sys.stdout.reconfigure(encoding="utf-8")` at import.

### `run_tests.py` pins the suites to `file`

Turning on `dynamodb` in `.env` silently re-pointed every suite that did not set
`STORE_BACKEND` itself at the real table. Three suites started failing on counts
they never wrote (`total questions: got 4, want 3` — production data), and, worse,
their resolve/delete steps were mutating live rows. `run_tests.py` now forces
`STORE_BACKEND=file` into each child's environment; suites that select a backend
themselves are unaffected.


### And the version that was still broken in production

Every store was in the table, the local dashboard was correct, and a calendar
period saved locally was *still* missing on `sc-assistant.online`. `STORE_BACKEND`
lived only in `.env` — which is gitignored, is not in the image, and is not one of
the `-e` flags in `cicd.yaml`. So the container read the variable, found nothing,
and took the `file` default: production wrote to its own writable layer while the
developer's machine wrote to DynamoDB. Two dashboards, two datasets, no error on
either side.

`config.py` now supplies the default (`STORE_BACKEND=dynamodb`) and exports it back
into `os.environ` so `rag/store.py` sees it. Defaulting in code beats adding a
ninth `-e` line: a missing env var fails silently and *looks like a UI bug*, and the
next person to add a store would inherit the same trap. `file` is still available
for offline work, and `run_tests.py` sets it explicitly.

`create_app()` also prints which backend is live at boot, so the next
misconfiguration is one `docker logs` away:

```
[startup] Admin state -> DynamoDB table 'SCAssistantStores' (shared)
[startup] WARNING: admin state -> local JSON files (NOT shared). ...
```

## Decisions worth defending

**Why `dynamodb` is the default and `file` is opt-in.** This is the reverse of the
original decision, and the reversal is the point: `file` was chosen as the default
so the app would start with no AWS account, but the cost turned out to be a
deployment that silently disagreed with the developer's machine for weeks. A
missing variable should not change where data goes. Offline runs opt out explicitly
(`run_tests.py` does), and section 8 of `test_store.py` proves the flip is
reversible with the file data intact.


**Why a shared backend and not a shared volume.** EFS would also make writes
visible, but `os.replace` plus a `threading.Lock` only serialises writers *inside
one process*. Two containers doing read-modify-write on the same file still lose
one of the two updates, and what disappears is an admin's decision. Per-item
storage means B writing `registrar|` cannot clobber A's `dean|college education`.

**Why one item per store instead of one item per row.** These datasets are
bounded (`MAX_VOTES = 2000`, `MAX_GAPS = 500`) and are almost always read whole:
`authority_block()` needs every resolution to match against a question, and the
admin screens list everything. One item is one round-trip instead of a scan, and
it preserves the load-mutate-save shape the callers already use — so
`_rebuild_topic()` and the prune helpers did not have to change at all.

The cost is the 400 KB item limit and last-writer-wins between two simultaneous
admins. Both are acceptable: 400 KB is far past the row caps, and two admins
resolving *the same key* in the same second is a coin flip under any design. What
matters is that neither silently loses the *other* key's decision.

**Why the payload is a JSON string, not a native map.** DynamoDB coerces numbers
to `Decimal` and rejects some empty values, which would force every caller to
sanitise its dict before saving. A blob round-trips byte-for-byte, which is what
"it behaves exactly like the file did" has to mean — `test_store.py` asserts
`n == 3` and not `Decimal('3')`, and that `Señor Peña — 👍` survives.

**Why `boto3` is imported lazily.** `rag.conflicts` is imported by the prompt
builder on every request. A module-level `import boto3` would make the whole RAG
pipeline — and every test that touches it — depend on boto3 being installed and
credentialled, in order to support a backend most runs never select.

**Why read failures are swallowed.** A throttle or a dropped connection must
degrade to "no decisions recorded", not a 500 on a student's question. Answering
matters more than bookkeeping; the exception is printed, not raised.

## Tests

`python tests/test_store.py` — 33 checks, no AWS, no network. A ~15-line `FakeTable`
implements only `get_item`/`put_item`, because that is the entire surface the code
uses; `moto` would be more faithful but not worth a new dependency for an offline
suite.

The sections that carry the argument:

- **6** — all four real modules (`set_resolution`, `record_gap`, `record_vote`,
  `create_escalation`) work unchanged on the DynamoDB backend, and the conflict
  decision still reaches the prompt.
- **7** — two `JsonBlobStore` objects standing in for two containers: B sees the
  decision A wrote, and neither clobbers the other's key.
- **8** — flipping back to `file` leaves the earlier file data untouched.

Registered first in `run_tests.py`: if the seam under all four stores is broken,
everything below it is a symptom.
