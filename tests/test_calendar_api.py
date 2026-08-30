"""
test_calendar_api.py — Academic Calendar admin endpoints.

    python tests/test_calendar_api.py

`test_calendar.py` proves the date arithmetic. This suite proves the parts only
the HTTP layer can get wrong:

* an unauthenticated request cannot read or edit the college's deadlines;
* a bad date is refused with a message an admin can act on, instead of being
  stored and silently producing "closes in -3 days" in a student's answer;
* `preview` returns the literal text the model will receive, for an arbitrary
  date — the one screen where the admin sees the *output* rather than their input;
* saving a period is immediately visible to `calendar_block()`, i.e. the admin
  edit and the assistant read the same store.

Storage is redirected to a temp file before any project import, and the heavy
imports (boto3, Pinecone, the embedding model) are stubbed, for the same reason
`test_feedback_api.py` stubs them: this is a test about HTTP behaviour and it
should not need credentials or a model download to run.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import datetime
import os
import shutil
import sys
import tempfile
import types

# ---------------------------------------------------------------- temp storage
_tmp = tempfile.mkdtemp(prefix="sccalendar_")
os.environ["STORE_BACKEND"] = "file"
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

from sc_assistant import create_app          # noqa: E402
from rag import calendar as cal              # noqa: E402

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


# ============================================================
section("1. Routes are registered")
# ============================================================
rules = sorted(str(r) for r in app.url_map.iter_rules())
for want in (
    "/admin/calendar/api/list",
    "/admin/calendar/api/save",
    "/admin/calendar/api/delete",
    "/admin/calendar/api/scan",
    "/admin/calendar/api/preview",
):
    check(f"{want} mounted", any(r.startswith(want) for r in rules))


# ============================================================
section("2. Only admins can touch the college's deadlines")
# ============================================================
# A wrong deadline is worse than a missing one, so write access is the thing to
# be strict about — but reads are restricted too, because an unpublished date is
# not public information.
check("anonymous cannot list", anon.get("/admin/calendar/api/list").status_code == 403)
check("student cannot list", student.get("/admin/calendar/api/list").status_code == 403)
check("student cannot save", student.post(
    "/admin/calendar/api/save",
    json={"label": "Enrollment", "start": "2026-06-01", "end": "2026-06-15"},
).status_code == 403)
check("student cannot delete", student.post(
    "/admin/calendar/api/delete", json={"key": "enrollment"}
).status_code == 403)
check("student cannot preview", student.post(
    "/admin/calendar/api/preview", json={"question": "when is enrollment?"}
).status_code == 403)
check("nothing was stored by those attempts", cal.list_periods() == [])


# ============================================================
section("3. Saving a period")
# ============================================================
r = admin.post("/admin/calendar/api/save", json={
    "label": "Enrollment",
    "start": "2026-06-01",
    "end": "2026-06-15",
    "note": "Late fee after June 10",
})
check("save accepted", r.status_code == 200, r.data[:160])
body = r.get_json()
check("success flag", body.get("success") is True)
check("period returned", body.get("period", {}).get("key") == "enrollment")
check("note kept", body["period"]["note"] == "Late fee after June 10")
check("who saved it is recorded", body["period"]["set_by"] == "admin@sc.edu.ph")
check("list comes back with it", len(body.get("periods", [])) == 1)

# Bad input has to be refused with a reason. Storing "June 15 to June 1" would
# produce a negative countdown in a student's answer, and the admin would have no
# idea which row caused it.
r = admin.post("/admin/calendar/api/save", json={
    "label": "Backwards", "start": "2026-06-15", "end": "2026-06-01",
})
check("end-before-start refused", r.status_code == 400)
check("and the message says why", "end" in r.get_json().get("message", "").lower())

r = admin.post("/admin/calendar/api/save", json={
    "label": "Vague", "start": "next tuesday", "end": "",
})
check("unparseable date refused", r.status_code == 400)

r = admin.post("/admin/calendar/api/save", json={"label": "", "start": "2026-06-01"})
check("missing label refused", r.status_code == 400)

r = admin.post("/admin/calendar/api/save", json={"label": "No Date", "start": ""})
check("missing start refused", r.status_code == 400)
check("no junk rows were created", len(cal.list_periods()) == 1)


# ============================================================
section("4. Listing classifies against a date the admin chooses")
# ============================================================
admin.post("/admin/calendar/api/save", json={
    "label": "Midterm Exams", "start": "2026-08-10", "end": "2026-08-14",
})

r = admin.get("/admin/calendar/api/list?today=2026-06-03")
body = r.get_json()
check("list ok", r.status_code == 200)
check("both periods listed", body["stats"]["total"] == 2)
check("enrollment is active on June 3", [p["label"] for p in body["state"]["active"]] == ["Enrollment"])
check("midterms are still upcoming", [p["label"] for p in body["state"]["upcoming"]] == ["Midterm Exams"])
check("days left computed", body["state"]["active"][0]["days_left"] == 12)

# The same data, a different day. This is what makes "will this still be correct
# in August?" answerable now instead of in August.
body = admin.get("/admin/calendar/api/list?today=2026-08-12").get_json()
check("in August, midterms are the active one", [p["label"] for p in body["state"]["active"]] == ["Midterm Exams"])
check("and enrollment has moved to past", [p["label"] for p in body["state"]["past"]] == ["Enrollment"])

r = admin.get("/admin/calendar/api/list?today=13/06/2026")
check("a malformed date override is refused, not ignored", r.status_code == 400)


# ============================================================
section("5. Preview shows the admin what the model is told")
# ============================================================
r = admin.post("/admin/calendar/api/preview", json={
    "question": "when is enrollment?", "today": "2026-06-03",
})
body = r.get_json()
check("preview ok", r.status_code == 200)
check("it applies", body["applies"] is True)
check("the block is the real thing", body["block"].startswith("<calendar>"))
check("status is spelled out for the model", "OPEN NOW" in body["block"])
check("the countdown is pre-computed", "12 day(s)" in body["block"])
check("the note travels with it", "Late fee" in body["block"])

# The day it closes, and the day after. These two are the whole feature.
body = admin.post("/admin/calendar/api/preview", json={
    "question": "can i still enroll?", "today": "2026-06-15",
}).get_json()
check("on the last day, it says so", "TODAY IS THE LAST DAY" in body["block"])

body = admin.post("/admin/calendar/api/preview", json={
    "question": "can i still enroll?", "today": "2026-06-16",
}).get_json()
check("the next day it is not open", "OPEN NOW" not in body["block"])
check("the next day it is reported as ended", "ALREADY ENDED" in body["block"])

# An empty block is a real answer, not a failure — the admin needs to be able to
# tell "no calendar context" apart from "the feature is broken".
body = admin.post("/admin/calendar/api/preview", json={
    "question": "who is the dean of CITAS?", "today": "2026-06-03",
}).get_json()
check("a non-timing question yields no block", body["block"] == "")
check("and preview says so plainly", body["applies"] is False and "No calendar context" in body["message"])

r = admin.post("/admin/calendar/api/preview", json={"question": ""})
check("empty question refused", r.status_code == 400)

r = admin.post("/admin/calendar/api/preview", json={"question": "when?", "today": "June 3"})
check("bad preview date refused", r.status_code == 400)


# ============================================================
section("6. Scanning pasted calendar text")
# ============================================================
r = admin.post("/admin/calendar/api/scan", json={
    "text": (
        "ACADEMIC CALENDAR SY 2026-2027\n"
        "Enrollment Period: June 1-15, 2026\n"
        "Start of Classes: June 22, 2026\n"
        "Christmas Break: December 20, 2026 - January 5, 2027\n"
        "The college was founded to serve the people of Samar.\n"
    ),
    "year": 2026,
})
body = r.get_json()
check("scan ok", r.status_code == 200)
found = body.get("found", [])
check("three ranges found", len(found) == 3, [f["label"] for f in found])
check("the prose line contributed nothing", all("founded" not in f["label"] for f in found))
check("enrollment range read correctly",
      any(f["start"] == "2026-06-01" and f["end"] == "2026-06-15" for f in found))
check("cross-year break read correctly",
      any(f["start"] == "2026-12-20" and f["end"] == "2027-01-05" for f in found))
# Already-confirmed rows are flagged so the UI can grey them out rather than
# inviting the admin to save the same period twice.
check("known rows are flagged as saved",
      any(f["already_saved"] for f in found if f["label"].lower().startswith("enrollment")))
check("scanning stored nothing on its own", len(cal.list_periods()) == 2)

check("empty paste refused", admin.post("/admin/calendar/api/scan", json={"text": "  "}).status_code == 400)
check("student cannot scan", student.post(
    "/admin/calendar/api/scan", json={"text": "June 1-15, 2026"}
).status_code == 403)


# ============================================================
section("7. Correcting and deleting")
# ============================================================
admin.post("/admin/calendar/api/save", json={
    "label": "enrollment", "start": "2026-06-01", "end": "2026-06-20",
})
check("a correction does not create a second row", len(cal.list_periods()) == 2)
check("the new end date took effect",
      next(p for p in cal.list_periods() if p["key"] == "enrollment")["end"] == "2026-06-20")
check("the note survived the correction",
      "Late fee" in next(p for p in cal.list_periods() if p["key"] == "enrollment")["note"])

r = admin.post("/admin/calendar/api/delete", json={"key": "midterm exams"})
check("delete ok", r.status_code == 200)
check("one period left", len(r.get_json()["periods"]) == 1)
check("deleting it again is a 404", admin.post(
    "/admin/calendar/api/delete", json={"key": "midterm exams"}
).status_code == 404)
check("delete needs a key", admin.post("/admin/calendar/api/delete", json={}).status_code == 400)


# ============================================================
section("8. The admin edit and the assistant read the same store")
# ============================================================
# The point of the whole feature: what the admin saved must reach the prompt with
# no re-index, no restart and no second copy of the data.
block = cal.calendar_block("when is enrollment?", today=datetime.date(2026, 6, 3))
check("the saved period reaches the prompt", "Enrollment" in block)
check("with the corrected end date", "June 20, 2026" in block)

admin.post("/admin/calendar/api/save", json={
    "label": "Enrollment", "start": "2026-06-01", "end": "2026-06-04",
})
block = cal.calendar_block("when is enrollment?", today=datetime.date(2026, 6, 3))
check("an edit is visible immediately", "June 4, 2026" in block)
check("and the countdown moved with it", "1 day(s)" in block)


print("\n" + "=" * 46)
print(f"  {_passed} passed, {_failed} failed")
print("=" * 46)

shutil.rmtree(_tmp, ignore_errors=True)
raise SystemExit(1 if _failed else 0)
