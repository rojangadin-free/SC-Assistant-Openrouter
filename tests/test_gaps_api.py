"""
test_gaps_api.py — integration test for the Content Gaps admin endpoints.

`test_gaps.py` proves the storage logic is right; this proves the HTTP layer
around it is wired correctly: the blueprint is reachable, non-admins are turned
away, and every button in the dashboard hits an endpoint that actually works.

The blueprint is mounted on a bare Flask app rather than the real
`create_app()`, because the real factory imports the whole RAG stack (Pinecone,
Bedrock, LangGraph) and would need live AWS credentials to import at all. What is
under test here is `admin_gaps.py`, so importing anything more than that would
only add ways for the test to fail for unrelated reasons.

Run:  python tests/test_gaps_api.py
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import os
import tempfile

_tmp = os.path.join(tempfile.gettempdir(), "sc_gaps_api_test.json")
os.environ["CONTENT_GAPS_FILE"] = _tmp
if os.path.exists(_tmp):
    os.remove(_tmp)

from flask import Flask                              # noqa: E402
from sc_assistant.admin_gaps import bp_gaps          # noqa: E402
from rag.gaps import record_gap                      # noqa: E402

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
app.register_blueprint(bp_gaps)


def login(client, role):
    """Set the session the way auth.py does after a successful login."""
    with client.session_transaction() as sess:
        sess["user"] = f"{role}@sc.edu"
        sess["role"] = role


# Seed two topics through the same function the chat route calls. The first two
# phrasings must land on ONE row — that grouping is the whole point of the
# feature, so the API test asserts it end-to-end rather than trusting it.
SHUTTLE_A = "Is there a shuttle service?"
SHUTTLE_B = "shuttle service po?"          # same topic words, different wording
record_gap(SHUTTLE_A, answer="I do not have that information.",
           asked_by="student1@sc.edu", conv_id="c1")
record_gap(SHUTTLE_B, answer="Not in my documents.", asked_by="student2@sc.edu")
record_gap("What is the dormitory fee?", answer="Not specified.", asked_by="student3@sc.edu")



print("\n=== 1. Routes are registered ===")
rules = sorted(str(r) for r in app.url_map.iter_rules() if "gaps" in str(r))
for r in rules:
    print(f"        {r}")
check("four gap endpoints mounted", len(rules), 4)


print("\n=== 2. Access control ===")
with app.test_client() as c:
    check("anonymous is refused", c.get("/admin/gaps/api/list").status_code, 403)

with app.test_client() as c:
    login(c, "user")
    check("logged-in non-admin is refused", c.get("/admin/gaps/api/list").status_code, 403)
    # A student must not be able to delete evidence that their question failed.
    check("non-admin cannot delete",
          c.post("/admin/gaps/api/delete", json={"key": "serv shuttl"}).status_code, 403)


print("\n=== 3. Admin can list ===")
with app.test_client() as c:
    login(c, "admin")
    r = c.get("/admin/gaps/api/list")
    check("200 OK", r.status_code, 200)
    body = r.get_json()
    check("success flag", body["success"], True)
    check("two topics returned", len(body["gaps"]), 2)
    check("most-asked first", body["gaps"][0]["count"], 2)
    check("stats: open topics", body["stats"]["open_topics"], 2)
    check("stats: total questions", body["stats"]["total_questions"], 3)

    key = body["gaps"][0]["key"]

    print("\n=== 4. Filters ===")
    check("status=resolved is empty for now",
          len(c.get("/admin/gaps/api/list?status=resolved").get_json()["gaps"]), 0)
    check("bad filter is rejected",
          c.get("/admin/gaps/api/list?status=banana").status_code, 400)

    print("\n=== 5. Resolve ===")
    r = c.post("/admin/gaps/api/resolve", json={"key": key, "note": "Uploaded shuttle schedule."})
    check("resolve 200", r.status_code, 200)
    body = r.get_json()
    check("status is resolved", body["gap"]["status"], "resolved")
    check("resolver is the logged-in admin", body["gap"]["resolved_by"], "admin@sc.edu")
    check("note stored", body["gap"]["note"], "Uploaded shuttle schedule.")
    check("open topics drops to 1", body["stats"]["open_topics"], 1)
    check("open filter now hides it",
          len(c.get("/admin/gaps/api/list?status=open").get_json()["gaps"]), 1)

    print("\n=== 6. Asked again -> reopens itself ===")
    # This is the behaviour that makes 'resolved' honest: a fix that did not
    # actually work must come back instead of staying silently closed.
    record_gap("is there any shuttle service?", answer="I do not have that information.")

    reopened = [g for g in c.get("/admin/gaps/api/list").get_json()["gaps"] if g["key"] == key]
    check("row is back to open", reopened[0]["status"], "open")
    check("reason is visible to the admin",
          "[Asked again after being marked resolved.]" in reopened[0]["note"], True)

    print("\n=== 7. Reopen / delete / bad input ===")
    c.post("/admin/gaps/api/resolve", json={"key": key})
    check("reopen 200", c.post("/admin/gaps/api/reopen", json={"key": key}).status_code, 200)
    check("missing key -> 400", c.post("/admin/gaps/api/resolve", json={}).status_code, 400)
    check("unknown key -> 404",
          c.post("/admin/gaps/api/resolve", json={"key": "no such topic"}).status_code, 404)
    check("delete 200", c.post("/admin/gaps/api/delete", json={"key": key}).status_code, 200)
    check("deleting twice -> 404",
          c.post("/admin/gaps/api/delete", json={"key": key}).status_code, 404)
    check("one topic left", len(c.get("/admin/gaps/api/list").get_json()["gaps"]), 1)


print(f"\n{'='*46}\n  {passed} passed, {failed} failed\n{'='*46}")
if os.path.exists(_tmp):
    os.remove(_tmp)
raise SystemExit(1 if failed else 0)
