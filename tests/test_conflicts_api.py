"""
test_conflicts_api.py — integration test for the Data Conflicts admin endpoints.

`check_data_conflicts.py` proves the *detection* works on the real PDFs. This
proves the admin screen's HTTP layer works: the routes exist, only admins can
touch them, a decision survives a round-trip, and the decision is actually the
one the answer pipeline will read.

The last check is the important one. A resolve endpoint that returns 200 but
whose decision never reaches `authority_block()` would look fine in the UI and
change nothing about the AI's answer — exactly the bug this feature exists to
prevent — so the test follows the decision all the way into the prompt text.

The blueprint is mounted on a bare Flask app: the real `create_app()` imports
Pinecone/Bedrock/LangGraph and cannot be imported without live AWS credentials.

Run:  python tests/test_conflicts_api.py
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import os
import tempfile

_tmp = os.path.join(tempfile.gettempdir(), "sc_conflicts_api_test.json")
os.environ["CONFLICT_RESOLUTIONS_FILE"] = _tmp
if os.path.exists(_tmp):
    os.remove(_tmp)

from flask import Flask                                      # noqa: E402
from sc_assistant.admin_conflicts import bp_conflicts        # noqa: E402
from rag.conflicts import authority_block, list_resolutions  # noqa: E402

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


app = Flask(__name__)
app.secret_key = "test-only"
app.register_blueprint(bp_conflicts)


def login(client, role):
    with client.session_transaction() as sess:
        sess["user"] = f"{role}@sc.edu"
        sess["role"] = role


# The real conflict from data/Samar-College-update.pdf.
KEY = "dean|college education"
RIGHT = "Jacqueline Montalis"
WRONG = "Dr. Nimfa T. Torremoro"


print("\n=== 1. Routes are registered ===")
rules = sorted(str(r) for r in app.url_map.iter_rules() if "conflicts" in str(r))
for r in rules:
    print(f"        {r}")
check("four conflict endpoints mounted", len(rules), 4)


print("\n=== 2. Access control ===")
with app.test_client() as c:
    check("anonymous cannot read", c.get("/admin/conflicts/api/resolutions").status_code, 403)
    # Letting a student pin "the correct dean" would be worse than the bug.
    check("anonymous cannot resolve",
          c.post("/admin/conflicts/api/resolve",
                 json={"key": KEY, "correct": "Anyone"}).status_code, 403)

with app.test_client() as c:
    login(c, "user")
    check("non-admin cannot resolve",
          c.post("/admin/conflicts/api/resolve",
                 json={"key": KEY, "correct": "Anyone"}).status_code, 403)


print("\n=== 3. Admin resolves the dean conflict ===")
with app.test_client() as c:
    login(c, "admin")

    check("starts with no decisions",
          len(c.get("/admin/conflicts/api/resolutions").get_json()["resolutions"]), 0)

    r = c.post("/admin/conflicts/api/resolve", json={
        "key": KEY,
        "correct": RIGHT,
        "rejected": [WRONG],
        "role": "dean",
        "subject": "College of Education",
        "note": "Confirmed with the registrar.",
    })
    check("resolve 200", r.status_code, 200)
    body = r.get_json()
    check("success", body["success"], True)
    res = body["resolution"]
    check("correct value stored", res["correct"], RIGHT)
    check("rejected value recorded", WRONG in res["rejected"], True)
    check("resolver recorded", res["resolved_by"], "admin@sc.edu")
    check("note recorded", res["note"], "Confirmed with the registrar.")

    check("decision is listed",
          len(c.get("/admin/conflicts/api/resolutions").get_json()["resolutions"]), 1)

    print("\n=== 4. Validation ===")
    check("missing key -> 400",
          c.post("/admin/conflicts/api/resolve", json={"correct": RIGHT}).status_code, 400)
    check("missing value -> 400",
          c.post("/admin/conflicts/api/resolve", json={"key": KEY}).status_code, 400)

    print("\n=== 5. The decision actually reaches the prompt ===")
    # This is the whole point: the stored decision must appear in the authority
    # block that gets prepended to the model's context, naming the correct value
    # AND the wrong one to ignore. Without this, the UI would be theatre.
    block = authority_block("who is the dean of the college of education?")
    check("authority block is produced", bool(block.strip()), True)
    check("correct value is asserted", RIGHT in block, True)
    check("wrong value is named as outdated", WRONG in block, True)
    print("        ---- prompt injection ----")
    for line in block.strip().splitlines():
        print("        " + line)

    # An unrelated question must not drag this in, or every answer would carry
    # noise about deans.
    check("unrelated question gets no block",
          authority_block("what are the library hours?").strip(), "")

    print("\n=== 6. Undo ===")
    r = c.post("/admin/conflicts/api/unresolve", json={"key": KEY})
    check("unresolve 200", r.status_code, 200)
    check("reports that something was removed", r.get_json()["removed"], True)
    check("decision list is empty again", len(list_resolutions()), 0)
    check("prompt no longer overrides", authority_block("dean of college of education").strip(), "")

    # Undo is idempotent on purpose: the admin's intent ("no decision for this
    # key") is already satisfied, so a second click is success, not an error.
    # The `removed` flag is what distinguishes the two cases.
    r = c.post("/admin/conflicts/api/unresolve", json={"key": KEY})
    check("undo is idempotent", r.status_code, 200)
    check("second undo removed nothing", r.get_json()["removed"], False)
    check("missing key -> 400",
          c.post("/admin/conflicts/api/unresolve", json={}).status_code, 400)



print(f"\n{'='*46}\n  {passed} passed, {failed} failed\n{'='*46}")
if os.path.exists(_tmp):
    os.remove(_tmp)
raise SystemExit(1 if failed else 0)
