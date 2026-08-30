# Shared storage for admin state (`rag/store.py`)

## The bug

Four modules keep admin state in JSON files beside the app:

| store | file | what is lost if it is not shared |
|---|---|---|
| `conflicts` | `conflict_resolutions.json` | the pinned correct value is not **enforced** |
| `gaps` | `content_gaps.json` | "asked 14 times" reads as "asked 5 times" |
| `feedback` | `answer_feedback.json` | votes split into two too-small samples |
| `escalations` | `escalations.json` | a student's question is never seen by the admin |

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

Switched by environment variable:

```bash
STORE_BACKEND=file       # default — identical to the old behaviour
STORE_BACKEND=dynamodb   # one item per store, shared by every instance
STORE_TABLE_NAME=SCAssistantStores   # optional
```

### Turning it on

```bash
python tools/create_stores_table.py     # creates the table AND copies the local JSON in
# then in .env:
STORE_BACKEND=dynamodb
```

The migration step matters: without it the switch looks like data loss, because
the app would start reading a table nobody has written to yet.

## Decisions worth defending

**Why `file` stays the default.** Local development, `run_tests.py` and the
defence demo all run one process with no AWS credentials. A storage layer that
needs a cloud account before the app will start is worse than one that does not
scale, and the env var makes the switch reversible — section 8 of `test_store.py`
proves the file data is still intact after a round trip through DynamoDB.

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
