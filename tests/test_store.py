"""
test_store.py — the storage seam behind conflicts / gaps / feedback / escalations.

What is actually being tested
-----------------------------
Not "does json.dump work". The claim this file has to defend is:

    switching STORE_BACKEND from `file` to `dynamodb` changes WHERE state lives
    and nothing else.

If that claim is wrong, the failure is the worst kind — an admin's decision is
accepted by the UI and then quietly not enforced. So the DynamoDB path is
exercised against a fake table (no AWS account, no network) to prove the four
modules behave identically on both backends, and a shared-backend case proves the
actual bug is fixed: a decision written by "instance A" is visible to "instance B".

Run standalone: `python tests/test_store.py`
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import os
import sys
import tempfile

# Must be set before the rag modules import, because each reads its file path at
# import time to build its default store.
_tmpdir = tempfile.mkdtemp(prefix="sc_store_test_")
os.environ["STORE_BACKEND"] = "file"
os.environ["CONFLICT_RESOLUTIONS_FILE"] = os.path.join(_tmpdir, "conflicts.json")
os.environ["CONTENT_GAPS_FILE"] = os.path.join(_tmpdir, "gaps.json")
os.environ["ANSWER_FEEDBACK_FILE"] = os.path.join(_tmpdir, "feedback.json")
os.environ["ESCALATIONS_FILE"] = os.path.join(_tmpdir, "escalations.json")

from rag import store as store_mod  # noqa: E402
from rag.store import JsonBlobStore  # noqa: E402

passed = failed = 0


def check(label, got, want):
    global passed, failed
    ok = got == want
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}\n        got:  {got!r}\n        want: {want!r}")


# ---------------------------------------------------------------------------
# A fake DynamoDB table.
#
# Only get_item/put_item are implemented because that is the entire surface
# `JsonBlobStore` uses — a mock that supports more than the code calls invites the
# test to drift away from the thing it is testing. `moto` would be more faithful,
# but adding a dependency to `requirements.txt` so the test suite can run offline
# is a worse trade than fifteen lines of dict.
# ---------------------------------------------------------------------------
class FakeTable:
    def __init__(self):
        self.items = {}
        self.reads = 0
        self.writes = 0
        self.fail_next_read = False

    def get_item(self, Key):
        self.reads += 1
        if self.fail_next_read:
            self.fail_next_read = False
            raise RuntimeError("ProvisionedThroughputExceededException (simulated)")
        item = self.items.get(Key["store"])
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.writes += 1
        self.items[Item["store"]] = dict(Item)


def use_fake_ddb(table):
    """Point the module at `table` and select the dynamodb backend."""
    os.environ["STORE_BACKEND"] = "dynamodb"
    store_mod.reset_cache()
    store_mod._table = lambda: table


def use_files():
    os.environ["STORE_BACKEND"] = "file"
    store_mod.reset_cache()


print("\n=== 1. The default is the local file (no AWS needed) ===")
use_files()
check("backend is file", store_mod.describe_backend()["backend"], "file")
check("not shared", store_mod.describe_backend()["shared_across_instances"], False)

s = JsonBlobStore("demo", os.path.join(_tmpdir, "demo.json"), lambda: {"rows": {}})
check("missing file -> default", s.load(), {"rows": {}})

s.save({"rows": {"a": 1}})
check("round-trips through the file", s.load()["rows"], {"a": 1})
check("file was actually written", os.path.exists(s.path), True)
# save() stamps updated_at so an admin screen can show when state last changed.
check("updated_at is stamped", bool(s.load().get("updated_at")), True)

# The default must be a fresh object each call. If it were a shared literal, one
# caller mutating "the empty store" would poison the next caller's empty store —
# a bug that only appears once two requests interleave.
empty1 = JsonBlobStore("d2", os.path.join(_tmpdir, "nope.json"), lambda: {"rows": {}}).load()
empty1["rows"]["leak"] = True
empty2 = JsonBlobStore("d3", os.path.join(_tmpdir, "nope2.json"), lambda: {"rows": {}}).load()
check("defaults are not shared between loads", empty2["rows"], {})


print("\n=== 2. A corrupt store degrades to empty instead of 500ing ===")
bad = os.path.join(_tmpdir, "corrupt.json")
with open(bad, "w", encoding="utf-8") as fh:
    fh.write("{this is not json")
s_bad = JsonBlobStore("corrupt", bad, lambda: {"rows": {}})
# Answering questions matters more than an admin's bookkeeping: a half-written
# file must not take the chat endpoint down with it.
check("corrupt file -> default", s_bad.load(), {"rows": {}})


print("\n=== 3. The DynamoDB path stores and reads the same dict ===")
table = FakeTable()
use_fake_ddb(table)

s_ddb = JsonBlobStore("demo", "unused-in-ddb-mode", lambda: {"rows": {}})
check("empty table -> default", s_ddb.load(), {"rows": {}})

s_ddb.save({"rows": {"dean": "Jacqueline Montalis"}, "n": 3})
check("one write", table.writes, 1)
check("keyed by store name", list(table.items), ["demo"])

got = s_ddb.load()
check("value survives", got["rows"]["dean"], "Jacqueline Montalis")
# Stored as a JSON string on purpose: DynamoDB would return ints as Decimal and
# reject some empty values, so a blob is what keeps "it behaves like the file did"
# literally true.
check("numbers stay ints, not Decimal", got["n"], 3)
check("payload is a string", isinstance(table.items["demo"]["payload"], str), True)

# Unicode is the realistic case here (ñ, é in names, emoji in a student's note).
s_ddb.save({"rows": {"note": "Señor Peña — 👍"}})
check("unicode round-trips", s_ddb.load()["rows"]["note"], "Señor Peña — 👍")


print("\n=== 4. A read failure does not propagate ===")
table.fail_next_read = True
# A throttle or dropped connection must degrade to "no decisions recorded" rather
# than turning into a failed answer for the student.
check("throttled read -> default", s_ddb.load(), {"rows": {}})
check("and the next read recovers", s_ddb.load()["rows"]["note"], "Señor Peña — 👍")


print("\n=== 5. mutate() is load->change->save, and can abort ===")
s_ddb.save({"rows": {}})
s_ddb.mutate(lambda d: d["rows"].update({"x": 1}))
check("mutation persisted", s_ddb.load()["rows"], {"x": 1})

writes_before = table.writes
s_ddb.mutate(lambda d: False)          # e.g. "delete something that isn't there"
check("aborted mutation writes nothing", table.writes, writes_before)


print("\n=== 6. The four real modules work on the DynamoDB backend ===")
# This is the actual claim: the swap is invisible to callers.
from rag import conflicts, escalation, feedback, gaps  # noqa: E402

table2 = FakeTable()
use_fake_ddb(table2)

KEY = "dean|college education"
conflicts.set_resolution(
    KEY, "Jacqueline Montalis",
    role="dean", subject="College of Education",
    rejected=["Dr. Nimfa T. Torremoro"], resolved_by="admin@sc.edu",
)
check("conflicts: stored", list(conflicts.list_resolutions()), [KEY])
# The point of the whole feature — the decision has to reach the prompt, not just
# the database.
block = conflicts.authority_block("who is the dean of the college of education?")
check("conflicts: reaches the prompt", "Jacqueline Montalis" in block, True)
check("conflicts: names the wrong value", "Nimfa" in block, True)

gaps.record_gap("is there a shuttle service?", answer="I do not have information")
gaps.record_gap("shuttle service po?")
check("gaps: one topic", gaps.gap_stats()["total_topics"], 1)
check("gaps: counted twice", gaps.gap_stats()["total_questions"], 2)

feedback.record_vote("m1", "down", question="what are the tuition fees?",
                     sources=["Samar-College-update.pdf p.12"])
check("feedback: vote stored", feedback.get_vote("m1")["verdict"], "down")
check("feedback: aggregate built", feedback.feedback_stats()["down"], 1)

esc = escalation.create_escalation("when is the entrance exam?",
                                   route="admissions",
                                   student_email="student@sc.edu")
check("escalations: queued", esc is not None, True)
check("escalations: pending", escalation.escalation_stats()["pending"], 1)

# Four stores, four items, one table.
check("all four stores are separate items",
      sorted(table2.items), ["conflicts", "escalations", "feedback", "gaps"])


print("\n=== 7. The bug that motivated this: two instances now agree ===")
# "instance A" and "instance B" are two independent JsonBlobStore objects — a
# separate process would look exactly like this to the table. On the file backend
# each container has its own writable layer and B simply never sees A's write;
# here they share the item, which is the entire point of the change.
shared = FakeTable()
use_fake_ddb(shared)

instance_a = JsonBlobStore("conflicts", "a.json", lambda: {"resolutions": {}})
instance_b = JsonBlobStore("conflicts", "b.json", lambda: {"resolutions": {}})

instance_a.save({"resolutions": {KEY: {"correct": "Jacqueline Montalis"}}})
seen_by_b = instance_b.load()["resolutions"].get(KEY, {}).get("correct")
check("B sees the decision A recorded", seen_by_b, "Jacqueline Montalis")

# And an unrelated key written by B does not clobber A's, which is the failure a
# shared *file* would still have (read-modify-write across processes).
instance_b.mutate(lambda d: d["resolutions"].update({"registrar|": {"correct": "Someone"}}))
after = instance_a.load()["resolutions"]
check("both keys survive", sorted(after), ["dean|college education", "registrar|"])


print("\n=== 8. Restoring the file backend leaves the earlier data intact ===")
use_files()
check("backend is file again", store_mod.describe_backend()["backend"], "file")
# Section 1 wrote this through the file backend; the DynamoDB detour must not have
# touched it. This is what makes the env var a safe rollback rather than a
# one-way door.
check("file data is still there", s.load()["rows"], {"a": 1})


print(f"\n{'='*46}\n  {passed} passed, {failed} failed\n{'='*46}")
raise SystemExit(1 if failed else 0)
