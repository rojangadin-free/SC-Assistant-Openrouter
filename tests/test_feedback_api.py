"""
test_feedback_api.py — Answer Quality (👍/👎) and the Ask-a-Human queue.

    python tests/test_feedback_api.py

Storage is redirected to temp files BEFORE any project import, so the real
answer_feedback.json / escalations.json are never touched. AWS is stubbed for the
same reason `test_gaps_api.py` stubs it: importing sc_assistant pulls in boto3
and Pinecone, and this suite is about HTTP behaviour, not infrastructure.

What is actually worth testing here
-----------------------------------
* a vote is stored WITH its sources — the sources are the whole reason this
  feature is more than a counter, so a vote that loses them is a silent failure;
* changing your vote overwrites it (👎 -> 👍 must not leave both);
* the regression export only contains genuinely clean topics — a "known-good"
  set that includes a downvoted answer would enshrine a bug as the expected
  result, which is worse than having no test set;
* an escalation with no way to reply is rejected;
* only admins can read student contact details or answer on the college's behalf.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import types

# ---------------------------------------------------------------- temp storage
_tmp = tempfile.mkdtemp(prefix="scfeedback_")
os.environ["ANSWER_FEEDBACK_FILE"] = os.path.join(_tmp, "answer_feedback.json")
os.environ["ESCALATIONS_FILE"] = os.path.join(_tmp, "escalations.json")
os.environ["CONTENT_GAPS_FILE"] = os.path.join(_tmp, "content_gaps.json")
os.environ["CONFLICT_RESOLUTIONS_FILE"] = os.path.join(_tmp, "conflict_resolutions.json")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("AWS_REGION", "us-east-1")

# (The old `sys.path.insert(dirname(__file__))` here is gone: it added the repo
#  root only while this file lived in the repo root. _bootstrap above does it
#  correctly from tests/.)

# ------------------------------------------------------- stub the heavy imports
# `sc_assistant.chat` imports rag.chain (Pinecone + a local embedding model) and
# aws.* (boto3). Neither is exercised by these endpoints; loading them would make
# the suite need credentials and a GPU-sized download to test a JSON round-trip.
_fake_chain = types.ModuleType("rag.chain")
_fake_chain.app_graph = types.SimpleNamespace(
    invoke=lambda *a, **k: {},
    update_state=lambda *a, **k: None,
    get_state=lambda *a, **k: None,
)
_fake_chain.chatModel = types.SimpleNamespace(stream=lambda *a, **k: iter(()))
# sc_assistant.chat streams through stream_answer() now, not chatModel.stream():
# a stream fails inside the caller's loop, where with_fallbacks can no longer
# reach it, so the provider walk had to move into an explicit generator.
_fake_chain.stream_answer = lambda *a, **k: iter(())

sys.modules["rag.chain"] = _fake_chain

_fake_s3 = types.ModuleType("aws.s3")
_fake_s3.get_s3_presigned_url = lambda *a, **k: None
_fake_s3.upload_file_to_s3 = lambda *a, **k: None
_fake_s3.delete_file_from_s3 = lambda *a, **k: None
_fake_s3.list_s3_files = lambda *a, **k: []
sys.modules["aws.s3"] = _fake_s3

_fake_ddb = types.ModuleType("aws.dynamodb")
for _name in (
    "upsert_conversation", "list_conversations", "get_conversation",
    "delete_conversation_from_db", "list_reports",
    "update_report_status", "save_file_metadata", "delete_file_from_db",
    "find_report_by_msg", "update_report_reason",
):
    setattr(_fake_ddb, _name, lambda *a, **k: None)

# save_report is recorded rather than ignored, so the suite can assert both halves
# of the rule: a bare 👎 must file nothing, and answering "what went wrong?" must
# file exactly one row. "The request returned 200" would pass either way.

_filed_reports = []
_fake_ddb.save_report = lambda data: (_filed_reports.append(dict(data)), "rid")[1]
# `admin.py` imports the table objects themselves, not just the helpers.
_fake_ddb.files_table = types.SimpleNamespace(scan=lambda *a, **k: {"Items": []})
_fake_ddb.conversations_table = types.SimpleNamespace(scan=lambda *a, **k: {"Items": []})
_fake_ddb.reports_table = types.SimpleNamespace(scan=lambda *a, **k: {"Items": []})
sys.modules["aws.dynamodb"] = _fake_ddb


_fake_students = types.ModuleType("aws.students")
_fake_students.get_student_by_uid = lambda *a, **k: None
_fake_students.get_student_by_email = lambda *a, **k: None
_fake_students.format_student_context = lambda *a, **k: ""
_fake_students.list_students = lambda *a, **k: []
sys.modules["aws.students"] = _fake_students

# Attribute access on this stub returns a no-op for ANY name. The alternative —
# enumerating every symbol the auth blueprint happens to import (`cognito_client`,
# `sign_up_user`, ...) — makes the test suite break every time that module grows
# a helper, which is a maintenance cost with no diagnostic value here.
class _AnyModule(types.ModuleType):
    def __getattr__(self, name):
        return lambda *a, **k: None


sys.modules["aws.cognito"] = _AnyModule("aws.cognito")

# `src.helper` imports langchain_core purely for message types the chat endpoint
# builds elsewhere; only `encode_image` is referenced by the code under test.
_fake_helper = types.ModuleType("src.helper")
_fake_helper.encode_image = lambda *a, **k: ""
_fake_helper.download_hugging_face_embeddings = lambda *a, **k: None
_fake_helper.load_pdf_file = lambda *a, **k: []
_fake_helper.text_split = lambda *a, **k: []
sys.modules["src.helper"] = _fake_helper

# boto3/botocore are not installed in this dev environment at all. The admin
# blueprint imports ClientError for exception handling it never reaches here, so a
# stand-in exception class is enough to let the app assemble.
_fake_botocore = types.ModuleType("botocore")
_fake_botocore_exc = types.ModuleType("botocore.exceptions")


class _ClientError(Exception):
    pass


_fake_botocore_exc.ClientError = _ClientError
_fake_botocore.exceptions = _fake_botocore_exc
sys.modules["botocore"] = _fake_botocore
sys.modules["botocore.exceptions"] = _fake_botocore_exc

sys.modules["boto3"] = _AnyModule("boto3")

# Pinecone (vector DB client) and the indexing entry point are imported by the
# admin blueprint for document upload — a code path no assertion in this file
# touches.
sys.modules["pinecone"] = _AnyModule("pinecone")

_fake_store_index = types.ModuleType("store_index")
_fake_store_index.append_file_to_index = lambda *a, **k: None
sys.modules["store_index"] = _fake_store_index

_fake_chain.embeddings = None






from sc_assistant import create_app                    # noqa: E402
from rag import feedback as fb                         # noqa: E402
from rag import escalation as esc                      # noqa: E402

# ------------------------------------------------------------------ tiny runner
_passed, _failed = 0, 0


def check(label, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -> {extra}" if extra else ""))


def section(title):
    print(f"\n=== {title} ===")


app = create_app()
app.config.update(TESTING=True)


def client_as(email=None, is_admin=False, guest=False):
    c = app.test_client()
    if email or guest:
        with c.session_transaction() as s:
            s["user"] = "guest" if guest else email
            s["uid"] = "uid-" + (email or "guest")
            s["is_guest"] = guest
            if is_admin:
                s["is_admin"] = True
                s["groups"] = ["admin"]
                s["role"] = "admin"
    return c


# ============================================================
section("1. Routes are registered")
# ============================================================
rules = sorted(str(r) for r in app.url_map.iter_rules())
for want in (
    "/chat/feedback",
    "/chat/escalate",
    "/admin/feedback/api/list",
    "/admin/feedback/api/escalations",
    "/admin/feedback/api/escalations/answer",
    "/admin/feedback/api/regression",
):
    check(f"{want} mounted", any(r.startswith(want) for r in rules))


# ============================================================
section("2. A student votes, and the sources come with it")
# ============================================================
stud = client_as("student@sc.edu.ph")

r = stud.post("/chat/feedback", json={
    "msg_id": "m1",
    "verdict": "down",
    "question": "How much is the tuition for BSIT?",
    "answer": "Tuition is PHP 12,000 per semester.",
    "sources": ["Samar-College-2024.pdf|p.40", "Samar-College-update.pdf|p.12"],
    "comment": "This is last year's rate.",
})
check("vote accepted", r.status_code == 200, r.data[:120])
check("success flag", r.get_json().get("success") is True)

vote = fb.get_vote("m1")
check("vote stored", vote is not None)
check("verdict recorded", vote and vote["verdict"] == "down")
check(
    "sources stored with the vote",
    vote and vote["sources"] == ["Samar-College-2024.pdf|p.40", "Samar-College-update.pdf|p.12"],
    vote and vote.get("sources"),
)
check("comment stored", vote and "last year" in vote["comment"])
check("voter recorded", vote and vote["voter"] == "student@sc.edu.ph")

# The thumb alone files nothing. Asserted because the opposite is invisible in
# the UI — the vote returns 200 either way — and the symptom of getting this
# wrong is an admin queue full of rows whose entire content is "someone disliked
# this", drowning the complaints that actually name a wrong figure.
check("a bare downvote files no report", len(_filed_reports) == 0, _filed_reports)
# The signal to the UI that the follow-up question is worth asking.
check("but the sheet is offered", r.get_json().get("can_report") is True, r.get_json())



# ============================================================
section("3. Changing your mind replaces the vote")
# ============================================================
stud.post("/chat/feedback", json={
    "msg_id": "m1",
    "verdict": "up",
    "question": "How much is the tuition for BSIT?",
    "answer": "Tuition is PHP 12,000 per semester.",
    "sources": ["Samar-College-update.pdf|p.12"],
})
stats = fb.feedback_stats()
check("still exactly one vote", stats["total_votes"] == 1, stats)
check("counted as up", stats["up"] == 1 and stats["down"] == 0, stats)
check("verdict flipped", fb.get_vote("m1")["verdict"] == "up")


# ============================================================
section("4. Guests may vote; bad input is refused")
# ============================================================
guest = client_as(guest=True)
r = guest.post("/chat/feedback", json={
    "msg_id": "m2", "verdict": "down",
    "question": "Is there a shuttle service?",
    "answer": "I do not have that information.",
    "sources": [],
})
check("guest vote accepted", r.status_code == 200, r.data[:120])
check("guest is labelled", fb.get_vote("m2")["voter"] == "guest")
# A report is a work order addressed to a person; a guest leaves nobody to
# address it to. So the sheet is not offered to them at all — collecting a reason
# that can never be filed would waste the one interaction they gave.
check("guest is not asked for a reason", r.get_json().get("can_report") is False, r.get_json())


check("anonymous is refused", app.test_client().post("/chat/feedback", json={
    "msg_id": "m3", "verdict": "up"}).status_code == 401)
check("bad verdict -> 400", stud.post("/chat/feedback", json={
    "msg_id": "m4", "verdict": "sideways"}).status_code == 400)
check("missing msg_id -> 400", stud.post("/chat/feedback", json={
    "verdict": "up"}).status_code == 400)


# ============================================================
section("5. The admin screen ranks failures first")
# ============================================================
admin = client_as("admin@sc.edu.ph", is_admin=True)

# Two more downvotes on one topic so ordering is unambiguous.
for i, mid in enumerate(("m5", "m6")):
    stud.post("/chat/feedback", json={
        "msg_id": mid, "verdict": "down",
        "question": "What are the enrollment requirements?",
        "answer": "Bring your form 138.",
        "sources": ["Samar-College-2024.pdf|p.9"],
    })

r = admin.get("/admin/feedback/api/list")
check("admin list 200", r.status_code == 200)
body = r.get_json()
topics = body["topics"]
check("worst topic first", topics[0]["down"] == 2, [(t["key"], t["down"]) for t in topics])
check("satisfaction computed", topics[0]["satisfaction"] == 0.0, topics[0].get("satisfaction"))
check(
    "suspect document named",
    topics[0]["suspect_sources"][0]["source"] == "Samar-College-2024.pdf|p.9",
    topics[0]["suspect_sources"],
)
check("stats: downvotes", body["stats"]["down"] == 3, body["stats"])
check("stats: satisfaction present", body["stats"]["satisfaction"] is not None)


# ============================================================
section("5b. Answering 'what went wrong?' is what files the report")
# ============================================================
# The admin's queue is built here and nowhere else, which is the point: every row
# in it was explained by a student. save_report() de-duplicates on msg_id, so
# changing the reason corrects that row instead of adding a second.
_before = len(_filed_reports)

r = stud.post("/chat/report", json={
    "msg_id": "m5",
    "reason": "Outdated / no longer true",
    "other_text": "",
    "msg_snippet": "Bring your form 138.",
    "question": "What are the enrollment requirements?",
    "sources": ["Samar-College-2024.pdf|p.9"],
})
check("reason accepted", r.status_code == 200, r.data[:160])
check("routed to the same table", len(_filed_reports) == _before + 1, len(_filed_reports))
check("same msg_id, so it updates not duplicates",
      _filed_reports[-1].get("msg_id") == "m5", _filed_reports[-1])
check("reason attached", _filed_reports[-1].get("reason") == "Outdated / no longer true",
      _filed_reports[-1])
# The row is only useful if it names the page to fix and the question that
# exposed it — a reason with no evidence sends the admin hunting.
check("the report names the document to fix",
      _filed_reports[-1].get("sources") == ["Samar-College-2024.pdf|p.9"],
      _filed_reports[-1].get("sources"))
check("and the question that exposed it",
      "requirement" in _filed_reports[-1].get("question", "").lower(),
      _filed_reports[-1].get("question"))
check("guests cannot file reports",
      guest.post("/chat/report", json={"msg_id": "m2", "reason": "Wrong information"}
                 ).status_code == 403)


r = admin.get("/admin/feedback/api/list?verdict=down")
check("verdict filter works", all(t["down"] > 0 for t in r.get_json()["topics"]))
check("bad filter -> 400", admin.get("/admin/feedback/api/list?verdict=maybe").status_code == 400)
check("non-admin cannot read", stud.get("/admin/feedback/api/list").status_code == 403)


# ============================================================
section("6. Regression export contains only clean topics")
# ============================================================
r = admin.get("/admin/feedback/api/regression")
cases = r.get_json()["cases"]
qs = [c["question"] for c in cases]
check("200 OK", r.status_code == 200)
check("the upvoted question is included", any("tuition" in q.lower() for q in qs), qs)
check("downvoted topics excluded", not any("requirement" in q.lower() for q in qs), qs)
check(
    "expected sources travel with the case",
    cases and cases[0]["expected_sources"] == ["Samar-College-update.pdf|p.12"],
    cases and cases[0],
)
check("non-admin cannot export", stud.get("/admin/feedback/api/regression").status_code == 403)


# ============================================================
section("7. Escalation: a student asks a human")
# ============================================================
r = stud.post("/chat/escalate", json={
    "question": "Is there a shuttle service from Catbalogan?",
    "route": "admin",
    "answer": "I do not have that information.",
})
check("escalate 200", r.status_code == 200, r.data[:160])
body = r.get_json()
check("success", body.get("success") is True)
check("tells the student who will reply", "reply to" in body["message"].lower(), body["message"])

items = esc.list_escalations()
check("queued", len(items) == 1)
check("reply address is the logged-in email", items[0]["reply_to"] == "student@sc.edu.ph")
check("status starts pending", items[0]["status"] == "pending")
check(
    "topic key matches the gap normalizer",
    items[0]["topic_key"] == __import__("rag.gaps", fromlist=["x"]).normalize_question(
        "Is there a shuttle service from Catbalogan?"),
)

check("unknown office -> 400", stud.post("/chat/escalate", json={
    "question": "x", "route": "canteen"}).status_code == 400)
check("empty question -> 400", stud.post("/chat/escalate", json={
    "question": "   "}).status_code == 400)


# ============================================================
section("8. A guest must leave a contact")
# ============================================================
r = guest.post("/chat/escalate", json={"question": "Do you offer night classes?"})
check("no contact -> 400", r.status_code == 400, r.data[:160])
check("message asks for one", "email or mobile" in r.get_json()["message"].lower())

r = guest.post("/chat/escalate", json={
    "question": "Do you offer night classes?",
    "contact": "0917 555 1234",
    "route": "admissions",
})
check("with a contact -> 200", r.status_code == 200, r.data[:160])
check("routed to admissions", "Admissions" in r.get_json()["message"], r.get_json()["message"])

r = guest.post("/chat/escalate", json={"question": "test", "contact": "not-a-contact"})
check("garbage contact -> 400", r.status_code == 400)


# ============================================================
section("9. Admin answers the queue")
# ============================================================
r = admin.get("/admin/feedback/api/escalations?status=pending")
check("admin sees the queue", r.status_code == 200)
q = r.get_json()
check("two pending", q["stats"]["pending"] == 2, q["stats"])
check("oldest first", q["escalations"][0]["question"].startswith("Is there a shuttle"))
check("routes exposed", "registrar" in q["routes"])
check("non-admin cannot see contacts", stud.get(
    "/admin/feedback/api/escalations").status_code == 403)

esc_id = q["escalations"][0]["id"]
r = admin.post("/admin/feedback/api/escalations/answer", json={
    "id": esc_id,
    "reply": "Yes — the shuttle leaves the Catbalogan terminal at 6:30 AM.",
})
check("answer 200", r.status_code == 200, r.data[:160])
item = r.get_json()["escalation"]
check("status is answered", item["status"] == "answered")
check("reply text kept", "6:30 AM" in item["reply"])
check("answered_by recorded", item["answered_by"] == "admin@sc.edu.ph")
check("pending drops to 1", r.get_json()["stats"]["pending"] == 1)

check("empty reply -> 400", admin.post("/admin/feedback/api/escalations/answer", json={
    "id": esc_id, "reply": "  "}).status_code == 400)
check("unknown id -> 404", admin.post("/admin/feedback/api/escalations/answer", json={
    "id": "nope", "reply": "hi"}).status_code == 404)
check("non-admin cannot answer", stud.post("/admin/feedback/api/escalations/answer", json={
    "id": esc_id, "reply": "I am not staff"}).status_code == 403)


# ============================================================
section("10. The student can read the reply")
# ============================================================
# The queue only has value if the answer reaches the person who asked. These
# checks are about that last hop, and about the fact that it must be scoped: an
# escalation inbox that shows other students' questions would leak both their
# problems and their contact details.
r = stud.get("/chat/escalations/mine")
check("mine 200", r.status_code == 200, r.data[:160])
mine = r.get_json()
check("only my own rows", len(mine["items"]) == 1, mine["items"])
check("the answered one is mine", mine["items"][0]["status"] == "answered")
check("reply is delivered", "6:30 AM" in mine["items"][0]["reply"])
check("counted as unread", mine["unread"] == 1, mine)
check(
    "office name is human-readable",
    mine["items"][0]["route_label"] == esc.ROUTES["admin"],
    mine["items"][0].get("route_label"),
)

check(
    "contact of others never exposed",
    "reply_to" not in mine["items"][0],
    list(mine["items"][0].keys()),
)

# The guest asked from a different session, so their row must not appear above —
# and must appear for them.
r = guest.get("/chat/escalations/mine")
gmine = r.get_json()
check("guest sees only theirs", len(gmine["items"]) == 1, gmine["items"])
check("guest row is the night-classes one",
      "night classes" in gmine["items"][0]["question"].lower())
check("a pending row still shows", gmine["items"][0]["status"] == "pending")
check("pending is not unread", gmine["unread"] == 0, gmine)

mine_id = mine["items"][0]["id"]
check("mark read 200", stud.post(f"/chat/escalations/{mine_id}/read").status_code == 200)
check("unread clears", stud.get("/chat/escalations/mine").get_json()["unread"] == 0)
check(
    "cannot mark someone else's as read",
    guest.post(f"/chat/escalations/{mine_id}/read").status_code == 404,
)
check("anonymous cannot read the inbox",
      app.test_client().get("/chat/escalations/mine").status_code == 401)


# ============================================================
section("10b. A reopened conversation remembers its votes")
# ============================================================
# The 👍/👎 only means anything if it survives a reload. It used to not: the id a
# vote was filed under was invented in the browser, so the same answer came back
# with a new id and the vote could never be found again — the student saw an
# un-pressed thumb and the honest conclusion was that their feedback was thrown
# away. These checks pin down the batch lookup that restores it.
fb.record_vote("restore-a", "up", question="What are the entrance requirements?")
fb.record_vote("restore-b", "down", question="How much is the tuition fee?")

r = stud.post("/chat/feedback/batch", json={"msg_ids": ["restore-a", "restore-b", "never-voted"]})
check("batch 200", r.status_code == 200, r.data[:160])
votes = r.get_json()["votes"]
check("both verdicts restored", votes == {"restore-a": "up", "restore-b": "down"}, votes)
check("an unvoted answer is simply absent", "never-voted" not in votes)

check("one read serves the whole thread",
      set(fb.get_votes(["restore-a", "restore-b"]).keys()) == {"restore-a", "restore-b"})

check("empty list -> empty map", stud.post(
    "/chat/feedback/batch", json={"msg_ids": []}).get_json()["votes"] == {})
check("a string instead of a list -> 400", stud.post(
    "/chat/feedback/batch", json={"msg_ids": "restore-a"}).status_code == 400)
check("anonymous cannot read votes", app.test_client().post(
    "/chat/feedback/batch", json={"msg_ids": ["restore-a"]}).status_code == 401)


# ============================================================
section("11. Dismiss, reopen, delete")
# ============================================================


r = admin.post("/admin/feedback/api/escalations/status", json={"id": esc_id, "status": "dismissed"})
check("dismiss 200", r.status_code == 200)
check("dismissed", r.get_json()["escalation"]["status"] == "dismissed")

r = admin.post("/admin/feedback/api/escalations/status", json={"id": esc_id, "status": "pending"})
check("reopen clears the stale reply", r.get_json()["escalation"]["reply"] == "",
      r.get_json()["escalation"])

check("bad status -> 400", admin.post("/admin/feedback/api/escalations/status", json={
    "id": esc_id, "status": "maybe"}).status_code == 400)

r = admin.post("/admin/feedback/api/escalations/delete", json={"id": esc_id})
check("delete 200", r.status_code == 200)
check("deleting twice -> 404", admin.post("/admin/feedback/api/escalations/delete", json={
    "id": esc_id}).status_code == 404)
check("one row left", len(esc.list_escalations()) == 1)

r = admin.post("/admin/feedback/api/delete", json={"key": topics[0]["key"]})
check("topic delete 200", r.status_code == 200)
check("its votes are gone too", all(
    v["topic_key"] != topics[0]["key"] for v in fb._load()["votes"].values()))
check("non-admin cannot delete", stud.post("/admin/feedback/api/delete", json={
    "key": "anything"}).status_code == 403)


# ============================================================
print("\n" + "=" * 46)
print(f"  {_passed} passed, {_failed} failed")
print("=" * 46)
sys.exit(1 if _failed else 0)
