"""
test_freshness_api.py — integration test for the document-freshness endpoints.

`test_freshness.py` proves the date logic. This proves the HTTP layer: the routes
exist, students cannot reach them, a date survives a round-trip, and — the one
that matters — the pre-index scan really does return the College of Education
contradiction from an uploaded PDF **without indexing anything**.

The blueprint is mounted on a bare Flask app: the real `create_app()` imports
Pinecone/Bedrock/LangGraph and cannot be imported without live credentials.

Run:  python tests/test_freshness_api.py
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import io
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="sc_freshness_api_")
os.environ["STORE_BACKEND"] = "file"
os.environ["DOC_FRESHNESS_FILE"] = os.path.join(_tmp, "doc_freshness.json")
os.environ["CONFLICT_RESOLUTIONS_FILE"] = os.path.join(_tmp, "conflicts.json")

from flask import Flask                                        # noqa: E402
from sc_assistant.admin_freshness import bp_freshness          # noqa: E402
from rag.freshness import get_doc_date                         # noqa: E402

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
app.register_blueprint(bp_freshness)
client = app.test_client()


def login(role):
    with client.session_transaction() as sess:
        sess["user"] = f"{role}@sc.edu"
        sess["role"] = role


def logout():
    with client.session_transaction() as sess:
        sess.clear()


print("\n=== 1. Routes are registered ===")
rules = sorted(str(r) for r in app.url_map.iter_rules() if "freshness" in str(r))
for r in rules:
    print(f"        {r}")
check("five freshness endpoints mounted", len(rules), 5)


print("\n=== 2. Only admins get in ===")
# Document dates decide which of two contradicting facts the assistant states, so
# write access here is write access to the answers students receive.
logout()
check("anonymous cannot list", client.get("/admin/freshness/api/list").status_code, 403)
check("anonymous cannot set a date",
      client.post("/admin/freshness/api/set-date",
                  json={"filename": "x.pdf"}).status_code, 403)
check("anonymous cannot scan",
      client.post("/admin/freshness/api/scan-upload").status_code, 403)

login("student")
check("a student cannot list", client.get("/admin/freshness/api/list").status_code, 403)
check("a student cannot set a date",
      client.post("/admin/freshness/api/set-date",
                  json={"filename": "x.pdf", "effective_date": "2025-08-01"}
                  ).status_code, 403)
check("nothing was written by the rejected calls", get_doc_date("x.pdf"), None)


print("\n=== 3. Setting and correcting a date ===")
login("admin")

r = client.post("/admin/freshness/api/set-date",
                json={"filename": "Samar-College-2024.pdf",
                      "effective_date": "2024-08-15",
                      "note": "old handbook"})
check("set-date returns 200", r.status_code, 200)
body = r.get_json()
check("it succeeded", body["success"], True)
check("the date is echoed back", body["document"]["effective_date"], "2024-08-15")
# Attribution matters: a wrong date needs a name attached to it, or nobody can be
# asked why the assistant now prefers the older document.
check("the admin is recorded", body["document"]["set_by"], "admin@sc.edu")
check("the date is really stored",
      get_doc_date("Samar-College-2024.pdf")["effective_date"], "2024-08-15")

r = client.post("/admin/freshness/api/set-date",
                json={"filename": "Samar-College-2024.pdf",
                      "effective_date": "2024-06-01"})
check("a correction returns 200", r.status_code, 200)
check("…and overwrites the old value",
      get_doc_date("Samar-College-2024.pdf")["effective_date"], "2024-06-01")

r = client.post("/admin/freshness/api/set-date",
                json={"filename": "bad.pdf", "effective_date": "15/01/2026"})
check("a non-ISO date is a 400, not a silent skip", r.status_code, 400)
check("…and explains the format", "YYYY-MM-DD" in r.get_json()["message"], True)
check("nothing was stored for it", get_doc_date("bad.pdf"), None)

r = client.post("/admin/freshness/api/set-date", json={})
check("a missing filename is a 400", r.status_code, 400)


print("\n=== 4. Listing ===")
client.post("/admin/freshness/api/set-date",
            json={"filename": "Samar-College-update.pdf",
                  "effective_date": "2025-08-01"})
client.post("/admin/freshness/api/set-date",
            json={"filename": "mystery-memo.pdf"})   # no date anywhere

r = client.get("/admin/freshness/api/list")
body = r.get_json()
check("list returns 200", r.status_code, 200)
check("all three documents are listed", body["stats"]["total"], 3)
# The actionable number: an undated file can neither win nor lose a comparison,
# so it will keep contradicting its neighbours silently.
check("the undated one is counted", body["stats"]["undated"], 1)
check("newest first",
      body["documents"][0]["key"], "samar-college-update.pdf")


print("\n=== 5. The prompt block, as the model will see it ===")
r = client.get("/admin/freshness/api/preview"
               "?sources=Samar-College-update.pdf,Samar-College-2024.pdf")
body = r.get_json()
check("preview returns 200", r.status_code, 200)
check("a block applies to these two", body["applies"], True)
check("it is the tagged block", "<document_dates>" in body["block"], True)
check("the newer file is marked NEWEST", "NEWEST" in body["block"], True)

r = client.get("/admin/freshness/api/preview?sources=mystery-memo.pdf")
body = r.get_json()
check("one source alone earns no block", body["applies"], False)
# An empty box has two very different causes; the admin must be told which.
check("…and the reason is spelled out",
      "fewer than two" in body["explanation"], True)


print("\n=== 6. The pre-index scan finds the real contradiction ===")
# A real PDF, built in memory. The scan path runs PyPDFLoader, so a fake byte
# string would only prove that error handling works.
try:
    from reportlab.pdfgen import canvas          # noqa: F401
    have_reportlab = True
except ImportError:
    have_reportlab = False

if have_reportlab:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 760, "STUDENT HANDBOOK")
    c.drawString(72, 740, "Effective August 2025")
    c.showPage()
    c.drawString(72, 760, "DEANS UPDATE")
    c.drawString(72, 740, "Jacqueline Montalis - College of Education")
    c.showPage()
    c.save()
    buf.seek(0)

    r = client.post(
        "/admin/freshness/api/scan-upload",
        data={
            "file": (buf, "Samar-College-update.pdf"),
            # What is already indexed: the 2024 handbook names Torremoro.
            "existing_text": "Dr. Nimfa T. Torremoro\n"
                             "Dean, College of Graduate Studies & College of Education\n",
            "existing_source": "Samar-College-2024.pdf",
        },
        content_type="multipart/form-data",
    )
    check("scan returns 200", r.status_code, 200)
    report = r.get_json()["report"]
    check("the date was read out of the PDF text",
          report["incoming_date"]["date"], "2025-08-01")
    check("both pages were scanned", report["pages_scanned"], 2)
    check("one contradiction found", report["summary"]["conflicts"], 1)

    conflict = report["conflicts"][0]
    check("it is the College of Education deanship",
          "education" in conflict["key"], True)
    check("the incoming value is Montalis",
          "Montalis" in conflict["incoming"]["value"], True)
    check("the existing value is Torremoro",
          "Torremoro" in conflict["existing"]["value"], True)
    check("freshness says the upload is newer",
          conflict["newer"], "Samar-College-update.pdf")
    # The scan is a DRY RUN. Recording the date here would mean a cancelled
    # upload had still changed how the assistant ranks its sources.
    check("scanning did NOT record the document",
          get_doc_date("Samar-College-update.pdf")["effective_date"], "2025-08-01")
else:
    print("  SKIP  reportlab not installed — PDF scan path not exercised")

r = client.post("/admin/freshness/api/scan-upload", data={})
check("scanning with no file is a 400", r.status_code, 400)

r = client.post(
    "/admin/freshness/api/scan-upload",
    data={"file": (io.BytesIO(b"this is not a pdf"), "broken.pdf")},
    content_type="multipart/form-data",
)
check("an unreadable file is a 500 with a message, not a crash", r.status_code, 500)
check("…and names the file", "broken.pdf" in r.get_json()["message"], True)


print("\n=== 7. Deleting ===")
r = client.post("/admin/freshness/api/delete", json={"filename": "mystery-memo.pdf"})
check("delete returns 200", r.status_code, 200)
check("it is gone", get_doc_date("mystery-memo.pdf"), None)

r = client.post("/admin/freshness/api/delete", json={"filename": "mystery-memo.pdf"})
check("deleting twice is a 404, not a 500", r.status_code, 404)

logout()
r = client.post("/admin/freshness/api/delete",
                json={"filename": "Samar-College-2024.pdf"})
check("anonymous cannot delete", r.status_code, 403)
check("…and the document survived",
      get_doc_date("Samar-College-2024.pdf")["effective_date"], "2024-06-01")


print("\n" + "=" * 60)
print(f"  {passed} passed, {failed} failed")
print("=" * 60)
raise SystemExit(1 if failed else 0)
