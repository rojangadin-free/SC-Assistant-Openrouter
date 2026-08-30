"""
test_gaps.py — verify content-gap detection, grouping and storage without
touching the real content_gaps.json or needing AWS/Pinecone.

Run:  python tests/test_gaps.py
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import os
import tempfile

# Point the store at a throwaway file BEFORE importing the module.
_tmp = os.path.join(tempfile.gettempdir(), "sc_gaps_test.json")
os.environ["CONTENT_GAPS_FILE"] = _tmp
if os.path.exists(_tmp):
    os.remove(_tmp)

from rag.gaps import (  # noqa: E402
    looks_unanswered, normalize_question, record_gap,
    list_gaps, gap_stats, set_gap_status, delete_gap,
)

passed = failed = 0


def check(label, got, want):
    global passed, failed
    ok = got == want
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")


print("\n=== 1. Refusal detection ===")
REFUSALS = [
    "I do not have information about the shuttle service schedule in my current documents.",
    "That detail is not specified in the documents provided.",
    "I could not find any information about dormitory fees.",
    "The documents do not mention a shuttle service.",
]
ANSWERS = [
    "The enrollment period runs from June 1 to June 15. [SOURCE: Samar-College-update.pdf | 12]",
    "Samar Colleges offers BSIT, BSCS and BSBA under the College of Computer Studies, and "
    "enrollment requires Form 138, a birth certificate and a good moral certificate. The "
    "registrar processes these on the ground floor during office hours. Note the exact fee "
    "table is not specified for graduate programs, but undergraduate tuition is listed in the "
    "handbook with the payment schedule and scholarship options.",
    "Yes, the library is open until 7:00 PM on weekdays.",
]
for a in REFUSALS:
    check(f"refusal -> True  | {a[:52]}…", looks_unanswered(a), True)
for a in ANSWERS:
    check(f"answer  -> False | {a[:52]}…", looks_unanswered(a), False)
check("empty string", looks_unanswered(""), False)


print("\n=== 2. Grouping (different phrasings -> one key) ===")
# Same topic word, different phrasing and language: these MUST collapse.
group_a = [
    "How do I enroll?",
    "paano po ba mag-enroll?",
    "Enrollment?",
    "enrolling",
]
keys_a = {normalize_question(q) for q in group_a}
for q in group_a:
    print(f"        {q!r:42} -> {normalize_question(q)!r}")
check("enroll phrasings collapse to 1 key", len(keys_a), 1)

# Adding a second topic word is a DIFFERENT, narrower question ("how to enroll"
# vs "enrollment requirements") and should stay its own row — grouping those
# together would hide what the student actually needed.
check(
    "extra topic word -> separate key",
    normalize_question("enrollment requirements") != normalize_question("how to enroll"),
    True,
)


group_b = ["enrollment requirements", "requirements for enrollment", "requirement to enroll po"]
keys_b = {normalize_question(q) for q in group_b}
for q in group_b:
    print(f"        {q!r:42} -> {normalize_question(q)!r}")
check("requirement phrasings collapse to 1 key", len(keys_b), 1)

check("greeting yields no key", normalize_question("hello po"), "hello")
check("pure particles yield empty key", normalize_question("po ba naman"), "")


print("\n=== 3. Recording and counting ===")
e1 = record_gap("Is there a shuttle service?", answer="I do not have that information.",
                asked_by="student1@sc.edu", conv_id="c1")
e2 = record_gap("shuttle services po?", answer="Not provided in my documents.",
                asked_by="student2@sc.edu", conv_id="c2")
check("same topic recorded twice -> count 2", e2["count"], 2)
check("same topic -> same key", e1["key"], e2["key"])
check("two distinct example phrasings kept", len(e2["examples"]), 2)

record_gap("What is the dormitory fee?", answer="Not specified.", asked_by="student3@sc.edu")
gaps = list_gaps()
check("two distinct topics stored", len(gaps), 2)
check("most-asked topic sorts first", gaps[0]["count"], 2)

check("meaningless question is not stored", record_gap("po ba"), None)


print("\n=== 4. Stats ===")
st = gap_stats()
check("total_topics", st["total_topics"], 2)
check("open_topics", st["open_topics"], 2)
check("total_questions", st["total_questions"], 3)


print("\n=== 5. Resolve / reopen / delete ===")
key = gaps[0]["key"]
r = set_gap_status(key, "resolved", resolved_by="admin@sc.edu", note="Added shuttle page.")
check("marked resolved", r["status"], "resolved")
check("resolver recorded", r["resolved_by"], "admin@sc.edu")
check("open_topics drops to 1", gap_stats()["open_topics"], 1)

# Asking the SAME topic again after it was marked fixed means the fix did not
# land, so the row must reopen itself and say so.
again = record_gap("shuttle service po?", answer="I do not know.")
check("same key as the resolved row", again["key"], key)
check("asked again -> auto-reopened", again["status"], "open")
check("reopen is noted for the admin", "[Asked again after being marked resolved.]" in again["note"], True)

check("delete removes the row", delete_gap(key), True)
check("only the other topic remains", len(list_gaps()), 1)
check("deleting a missing key is safe", delete_gap("nope nothing here"), False)



print(f"\n{'='*46}\n  {passed} passed, {failed} failed\n{'='*46}")
if os.path.exists(_tmp):
    os.remove(_tmp)
raise SystemExit(1 if failed else 0)
