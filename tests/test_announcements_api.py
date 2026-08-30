"""
test_announcements_api.py — Announcements endpoints, including the public one.

    python tests/test_announcements_api.py

`test_announcements.py` proves the live-window logic. This suite proves the parts
only the HTTP layer can get wrong, and one of them is a security boundary rather
than a bug:

* **the public banner endpoint works for a guest** — that route is public on
  purpose (someone checking "is the campus open?" during a typhoon has no
  account), so it has to be verified that it answers without a session *and*
  that being public did not quietly turn it into a leak;
* **audience is decided server-side.** A student appending `?audience=faculty`
  must not be promoted. This is the failure that no amount of unit testing on
  `pinned_announcements()` can catch, because the function itself is correct —
  the question is who gets to choose its argument;
* `posted_by` never reaches a student: staff names are not part of a notice;
* only admins can post, take down or delete;
* `preview` returns the literal text the model receives, so an announcement that
  reads perfectly in the form but reaches nobody is visible before a storm rather
  than after;
* an admin edit is immediately visible to `announcement_block()` — the admin
  screen and the assistant read the same store.

Storage is redirected to a temp file before any project import, and the heavy
imports (boto3, Pinecone, the embedding model) are stubbed — same reasoning as
`test_calendar_api.py`: this is a test about HTTP behaviour and should not need
credentials or a model download.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import datetime
import os
import sys
import tempfile
import types


# ---------------------------------------------------------------- temp storage
_tmp = tempfile.mkdtemp(prefix="scann_")
os.environ["STORE_BACKEND"] = "file"
os.environ["ANNOUNCEMENTS_FILE"] = os.path.join(_tmp, "announcements.json")
os.environ["CALENDAR_PERIODS_FILE"] = os.path.join(_tmp, "calendar_periods.json")
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
_fake_chain = types.ModuleType("rag.chain")
_fake_chain.app_graph = types.SimpleNamespace(
    invoke=lambda *a, **k: {},
    update_state=lambda *a, **k: None,
    get_state=lambda *a, **k: None,
)
_fake_chain.chatModel = types.SimpleNamespace(stream=lambda *a, **k: iter(()))
# The chat route streams through stream_answer() now — see rag/chain.py.
_fake_chain.stream_answer = lambda *a, **k: iter(())

_fake_chain.embeddings = None
sys.modules["rag.chain"] = _fake_chain

_fake_s3 = types.ModuleType("aws.s3")
for _n in ("get_s3_presigned_url", "upload_file_to_s3", "delete_file_from_s3"):
    setattr(_fake_s3, _n, lambda *a, **k: None)
_fake_s3.list_s3_files = lambda *a, **k: []
sys.modules["aws.s3"] = _fake_s3

_fake_ddb = types.ModuleType("aws.dynamodb")
for _n in (
    "upsert_conversation", "list_conversations", "get_conversation",
    "delete_conversation_from_db", "list_reports", "update_report_status",
    "save_file_metadata", "delete_file_from_db", "find_report_by_msg",
    "update_report_reason", "save_report",
):
    setattr(_fake_ddb, _n, lambda *a, **k: None)
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


class _AnyModule(types.ModuleType):
    """Any attribute is a no-op — see the note in test_feedback_api.py."""

    def __getattr__(self, name):
        return lambda *a, **k: None


sys.modules["aws.cognito"] = _AnyModule("aws.cognito")

_fake_helper = types.ModuleType("src.helper")
_fake_helper.encode_image = lambda *a, **k: ""
_fake_helper.download_hugging_face_embeddings = lambda *a, **k: None
_fake_helper.load_pdf_file = lambda *a, **k: []
_fake_helper.text_split = lambda *a, **k: []
sys.modules["src.helper"] = _fake_helper

_fake_botocore = types.ModuleType("botocore")
_fake_botocore_exc = types.ModuleType("botocore.exceptions")


class _ClientError(Exception):
    pass


_fake_botocore_exc.ClientError = _ClientError
_fake_botocore.exceptions = _fake_botocore_exc
sys.modules["botocore"] = _fake_botocore
sys.modules["botocore.exceptions"] = _fake_botocore_exc
sys.modules["boto3"] = _AnyModule("boto3")
sys.modules["pinecone"] = _AnyModule("pinecone")

_fake_store_index = types.ModuleType("store_index")
_fake_store_index.append_file_to_index = lambda *a, **k: None
sys.modules["store_index"] = _fake_store_index

from sc_assistant import create_app        # noqa: E402
from rag import announcements as ann       # noqa: E402
from rag.calendar import today_in_manila   # noqa: E402


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


def client_as(email=None, is_admin=False):
    c = app.test_client()
    if email:
        with c.session_transaction() as s:
            s["user"] = email
            s["uid"] = "uid-" + email
            if is_admin:
                s["is_admin"] = True
                s["groups"] = ["admin"]
                s["role"] = "admin"
    return c


admin = client_as("admin@sc.edu.ph", is_admin=True)
student = client_as("student@sc.edu.ph")
anon = client_as()

# ---------------------------------------------------------------- date fixtures
#
# The public banner endpoint takes NO `today` override, and that is deliberate:
# a student's browser must never be able to argue about what day it is on campus.
# The consequence for this suite is that fixtures cannot use hard-coded dates —
# they would drift out of their own window and the whole file would start failing
# on an arbitrary future afternoon, which is the least useful kind of red test.
#
# So the dates are anchored to the campus clock, and the admin endpoints (which
# DO accept an override) are still driven with explicit days so the boundary
# behaviour is pinned rather than approximated.
TODAY = today_in_manila()
TOMORROW = TODAY + datetime.timedelta(days=1)
DAY_AFTER = TODAY + datetime.timedelta(days=2)
LONG_GONE = TODAY - datetime.timedelta(days=30)

SUSPENSION = "Classes are suspended today"
CORRECTED = "Classes are suspended today and tomorrow"
FACULTY = "Faculty payroll meeting at 4pm"



# ============================================================
section("1. Routes are registered")
# ============================================================
rules = sorted(str(r) for r in app.url_map.iter_rules())
for want in (
    "/api/announcements",
    "/admin/announcements/api/list",
    "/admin/announcements/api/save",
    "/admin/announcements/api/delete",
    "/admin/announcements/api/toggle",
    "/admin/announcements/api/preview",
):
    check(f"{want} mounted", any(r.startswith(want) for r in rules))


# ============================================================
section("2. Only admins can post to the whole campus")
# ============================================================
check("anonymous cannot list", anon.get("/admin/announcements/api/list").status_code == 403)
check("student cannot list", student.get("/admin/announcements/api/list").status_code == 403)
check("student cannot post",
      student.post("/admin/announcements/api/save",
                   json={"title": "No classes, everyone go home"}).status_code == 403)
check("student cannot take down",
      student.post("/admin/announcements/api/toggle",
                   json={"key": "x", "active": False}).status_code == 403)
check("student cannot delete",
      student.post("/admin/announcements/api/delete", json={"key": "x"}).status_code == 403)
check("student cannot preview the prompt",
      student.post("/admin/announcements/api/preview", json={}).status_code == 403)

# The point of the previous block: nothing was created by any of those attempts.
check("no rows were created by the refused attempts", len(ann.list_announcements()) == 0)


# ============================================================
section("3. Posting one")
# ============================================================
r = admin.post("/admin/announcements/api/save", json={
    "title": SUSPENSION,
    "body": "Typhoon signal no. 2. All offices closed.",
    "priority": "urgent",
    "audience": "all",
    "starts_on": TODAY.isoformat(),
    "expires_on": TODAY.isoformat(),
    "pinned": True,
})

check("save accepted", r.status_code == 200, r.status_code)
j = r.get_json()
check("success flag", j.get("success") is True)
suspension = j.get("announcement") or {}
check("record returned", bool(suspension.get("key")))
check("urgency kept", suspension.get("priority") == "urgent")
check("who posted it is recorded", suspension.get("posted_by") == "admin@sc.edu.ph")

# A faculty-only notice, used below to prove the audience boundary.
r = admin.post("/admin/announcements/api/save", json={
    "title": FACULTY,
    "audience": "faculty",
    "priority": "info",
    "pinned": True,
})

check("faculty notice posted", r.get_json().get("success") is True)
faculty_key = r.get_json()["announcement"]["key"]


# ============================================================
section("4. Dates that cannot be honoured are refused, with a reason")
# ============================================================
# Storing these would produce "in effect until ... (-3 more day(s))" in a
# student's answer, which is worse than refusing the form.
r = admin.post("/admin/announcements/api/save", json={
    "title": "Backwards",
    "starts_on": DAY_AFTER.isoformat(),
    "expires_on": TODAY.isoformat(),
})

check("end-before-start refused", r.status_code == 400, r.status_code)
check("and the message says why", "date" in (r.get_json().get("message") or "").lower())

r = admin.post("/admin/announcements/api/save",
               json={"title": "Bad date", "starts_on": "22-08-2026"})
check("unparseable date refused", r.status_code == 400)

r = admin.post("/admin/announcements/api/save", json={"title": "   "})
check("blank headline refused", r.status_code == 400)
check("headline message is actionable",
      "headline" in (r.get_json().get("message") or "").lower())

check("still only the two real rows", len(ann.list_announcements()) == 2)


# ============================================================
section("5. The public banner endpoint")
# ============================================================
# Public on purpose. A prospective student checking "is the campus open?" during
# a storm is exactly who the notice is for, and they have no account.
r = anon.get("/api/announcements")
check("200 for a guest", r.status_code == 200, r.status_code)
body = r.get_json()
check("success flag", body.get("success") is True)
titles = [a["title"] for a in body.get("announcements", [])]
check("the suspension is pinned for a guest", SUSPENSION in titles)
check("faculty notice is NOT sent to a guest", FACULTY not in titles)

first = next(a for a in body["announcements"] if a["title"] == SUSPENSION)
check("headline present", bool(first.get("title")))
check("body present", "Typhoon" in (first.get("body") or ""))
check("priority present, so the banner can shout", first.get("priority") == "urgent")
check("end date present, so 'until when' is answerable",
      first.get("expires_on") == TODAY.isoformat())

# Staff names have no reason to leave the admin screen.
check("posted_by withheld from the public payload", "posted_by" not in first)

# The bug: dismissals live in localStorage, which is per-BROWSER, not per-account.
# On a shared campus PC one student dismissing a suspension used to hide it from
# the next person to log in — a student who was never told classes were cancelled.
# The payload carries a per-account tag so the client can namespace its keys.
student_viewer = student.get("/api/announcements").get_json().get("viewer")
other = client_as("other@sc.edu.ph").get("/api/announcements").get_json().get("viewer")
guest_viewer = body.get("viewer")

check("a viewer tag is sent", bool(student_viewer))
check("two accounts get different tags", student_viewer != other,
      f"{student_viewer} == {other}")
check("the same account is stable across polls",
      student_viewer == student.get("/api/announcements").get_json().get("viewer"))
check("a guest is tagged too", guest_viewer == "guest")
check("the tag is not the email", "@" not in str(student_viewer))



# ============================================================
section("6. Audience comes from the session, not the URL")
# ============================================================
# The failure being guarded against: a student reads internal notices by editing
# a URL. `pinned_announcements()` is correct either way — the question is who
# gets to choose its argument.
r = anon.get("/api/announcements?audience=faculty")
titles = [a["title"] for a in r.get_json()["announcements"]]
check("a guest cannot promote themselves with a query parameter", FACULTY not in titles)

r = student.get("/api/announcements?audience=faculty")
titles = [a["title"] for a in r.get_json()["announcements"]]
check("neither can a logged-in student", FACULTY not in titles)

r = admin.get("/api/announcements")
titles = [a["title"] for a in r.get_json()["announcements"]]
check("staff do see the faculty notice", FACULTY in titles)
check("and the campus-wide one too", SUSPENSION in titles)


# ============================================================
section("7. The admin list says what is actually live")
# ============================================================
# `live` is computed server-side because "is this still showing?" depends on
# campus time, not on the admin's laptop clock.
r = admin.get(f"/admin/announcements/api/list?today={TODAY.isoformat()}")
check("list ok", r.status_code == 200)
j = r.get_json()
rows = {a["title"]: a for a in j["announcements"]}
check("both rows listed", len(rows) == 2)
check("the suspension is live today", rows[SUSPENSION]["live"])
check("and not expired", rows[SUSPENSION]["expired"] is False)
check("stats count one urgent", j["stats"]["urgent"] == 1)

# The inclusive end date, seen through the API: a one-day suspension must survive
# its own final day, which is the boundary an exclusive comparison gets wrong.
r = admin.get(f"/admin/announcements/api/list?today={TOMORROW.isoformat()}")
rows = {a["title"]: a for a in r.get_json()["announcements"]}
check("expired the day after", rows[SUSPENSION]["expired"])
check("and no longer live", rows[SUSPENSION]["live"] is False)


r = admin.get("/admin/announcements/api/list?today=nonsense")
check("a malformed date override is refused, not ignored", r.status_code == 400)


# ============================================================
section("8. Preview shows the literal text the model gets")
# ============================================================
r = admin.post("/admin/announcements/api/preview",
               json={"audience": "students", "today": TODAY.isoformat()})
check("preview ok", r.status_code == 200)
j = r.get_json()
check("it applies", j.get("applies") is True)
block = j.get("block") or ""
check("the block is the real thing", SUSPENSION in block)
check("urgency is marked for the model", "[URGENT]" in block)
check("the override instruction is present", "OVERRIDE" in block)
check("the faculty notice is not in the student block", FACULTY not in block)

# The case an admin most needs to see before a storm: posted, but reaching nobody.
r = admin.post("/admin/announcements/api/preview",
               json={"audience": "students", "today": DAY_AFTER.isoformat()})
j = r.get_json()

check("after it expires, nothing applies", j.get("applies") is False)
check("and preview says so plainly", "documents alone" in (j.get("message") or ""))

r = admin.post("/admin/announcements/api/preview", json={"audience": "martians"})
check("unknown audience refused", r.status_code == 400)
r = admin.post("/admin/announcements/api/preview", json={"today": "22-08-2026"})
check("bad preview date refused", r.status_code == 400)


# ============================================================
section("9. Take down, put back")
# ============================================================
# The fast path when a suspension is lifted: it must stop reaching students
# immediately, and deleting the evidence is the wrong way to do that.
r = admin.post("/admin/announcements/api/toggle",
               json={"key": suspension["key"], "active": False})
check("toggle ok", r.status_code == 200)
check("message confirms it is down", "down" in r.get_json()["message"].lower())

titles = [a["title"] for a in anon.get("/api/announcements").get_json()["announcements"]]
check("banner is gone at once", SUSPENSION not in titles)

blk = admin.post("/admin/announcements/api/preview",
                 json={"audience": "students", "today": TODAY.isoformat()}).get_json()["block"]
check("and the model is no longer told", SUSPENSION not in blk)

# But the record survives, because "what were students told, and when?" is the
# first question after an incident.
rows = {a["title"]: a for a in admin.get("/admin/announcements/api/list").get_json()["announcements"]}
check("the record is kept for the audit trail", SUSPENSION in rows)

r = admin.post("/admin/announcements/api/toggle",
               json={"key": suspension["key"], "active": True})
check("put back ok", r.status_code == 200)
titles = [a["title"] for a in anon.get("/api/announcements").get_json()["announcements"]]
check("banner returns", SUSPENSION in titles)


check("toggle needs a key",
      admin.post("/admin/announcements/api/toggle", json={"active": False}).status_code == 400)
check("toggle needs the flag",
      admin.post("/admin/announcements/api/toggle",
                 json={"key": suspension["key"]}).status_code == 400)
check("toggling a ghost is a 404",
      admin.post("/admin/announcements/api/toggle",
                 json={"key": "nope", "active": True}).status_code == 404)


# ============================================================
section("10. A correction edits in place")
# ============================================================
# Two live rows for one event is exactly the contradiction this whole feature set
# exists to remove, so an edit must not create a second announcement.
r = admin.post("/admin/announcements/api/save", json={
    "key": suspension["key"],
    "title": CORRECTED,
    "body": "Typhoon signal no. 2. All offices closed.",
    "priority": "urgent",
    "audience": "all",
    "starts_on": TODAY.isoformat(),
    "expires_on": TOMORROW.isoformat(),
    "pinned": True,
})
check("edit accepted", r.status_code == 200)
check("no second row appeared", len(ann.list_announcements()) == 2)

titles = [a["title"] for a in anon.get("/api/announcements").get_json()["announcements"]]
check("students see the corrected headline", CORRECTED in titles)
check("and not the old one", SUSPENSION not in titles)

blk = admin.post("/admin/announcements/api/preview",
                 json={"audience": "students",
                       "today": TOMORROW.isoformat()}).get_json()["block"]
check("the extended window took effect in the prompt", CORRECTED in blk)



# ============================================================
section("11. Delete")
# ============================================================
r = admin.post("/admin/announcements/api/delete", json={"key": faculty_key})
check("delete ok", r.status_code == 200)
check("one row left", len(ann.list_announcements()) == 1)
check("deleting it again is a 404",
      admin.post("/admin/announcements/api/delete", json={"key": faculty_key}).status_code == 404)
check("delete needs a key",
      admin.post("/admin/announcements/api/delete", json={}).status_code == 400)


# ============================================================
section("12. Admin screen and assistant read the same store")
# ============================================================
# The bug this catches: an admin posts, the dashboard lists it, and the model is
# never told — two stores, or a stale cache, and nobody notices until a storm.
block = ann.announcement_block(audience="students", today=TODAY)
check("what was posted over HTTP reaches the prompt function", CORRECTED in block)



print(f"\n{'='*60}")
print(f"  {_passed} passed, {_failed} failed")
print("=" * 60)

import shutil  # noqa: E402
shutil.rmtree(_tmp, ignore_errors=True)

raise SystemExit(1 if _failed else 0)
