"""
tools/ui_preview.py — a design-review harness for the real templates.

    python tools/ui_preview.py        ->  http://localhost:5055

WHY THIS EXISTS
---------------
`run.py` boots the whole product: Cognito, DynamoDB, S3, Pinecone, a
cross-encoder. That is the right thing for the product and the wrong thing for
looking at the UI — a machine without AWS credentials (or without boto3
installed at all) cannot render a single page, so nobody can see the screen they
are being asked to fix.

This serves the SAME Jinja templates and the SAME stylesheets out of
sc_assistant/, with the data faked at the seams the templates actually touch:
url_for endpoints, the `user` object, and the handful of JSON endpoints the page
calls on load. Nothing here is imported by the app; deleting this file changes
nothing about production.

It is deliberately NOT a test double for the API. Payload shapes are copied from
what the front-end reads (see chat.js) only as far as is needed to make the page
look like a page: a conversation list with entries in it, populated dashboard
tables, and an answer that streams in token by token. Those are the
states worth reviewing, and they are exactly the states you never see on an
empty dev database.
"""

import json
import os
import sys
import time

# The harness lives in tools/, the app lives one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response, jsonify, render_template, request
from jinja2 import ChainableUndefined

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "sc_assistant")

app = Flask(
    __name__,
    template_folder=os.path.join(PKG, "templates"),
    static_folder=os.path.join(PKG, "static"),
)

# The templates reach into objects the real views build from DynamoDB. Anything
# this harness has not thought to fake would otherwise raise UndefinedError and
# 500 the page — which turns "review the design" into "guess the context dict".
# ChainableUndefined renders a missing value as empty and lets attribute access
# keep chaining, so a page with one unstubbed field still paints.
app.jinja_env.undefined = ChainableUndefined

# Flask ties template reloading to debug mode, which is off here (the reloader
# fights with running this in the background). Without this, a template edit is
# invisible until the process is restarted — and silently reviewing the previous
# version of a page is worse than not reviewing it.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


# Mirrors sc_assistant/__init__.py, so asset URLs look the same here as in prod.
@app.url_defaults
def _static_cache_bust(endpoint, values):
    if endpoint != "static" or "filename" not in values:
        return
    try:
        values["v"] = int(os.stat(os.path.join(app.static_folder, values["filename"])).st_mtime)
    except OSError:
        pass


USER = {
    "username": "Juan Dela Cruz",
    "email": "juan.delacruz@samarcollege.edu.ph",
    "is_guest": False,
    "avatar_url": "",
    "role": "admin",
}
GUEST = {"username": "Guest", "email": "", "is_guest": True, "avatar_url": "", "role": "guest"}
STUDENT = {**USER, "role": "student"}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints the templates name in url_for(). The names matter, the paths do not.
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/chat", endpoint="chat.chat_page")
def index():
    return render_template("chat.html", user=STUDENT, start_new="true")


app.add_url_rule("/", endpoint="chat.index", view_func=index)


@app.route("/guest-chat")
def guest_chat():
    return render_template("chat.html", user=GUEST, start_new="true")


@app.route("/login", endpoint="auth.auth_page")
def login():
    return render_template("auth.html")


@app.get("/signup", endpoint="auth.signup")
def signup():
    return render_template("auth.html", initial_view="signup")


@app.get("/password-reset", endpoint="auth.password_reset_page")
def password_reset():
    return render_template("auth.html", initial_view="reset", reset_state={})


@app.get("/__ui_preview__/health")
def preview_health():
    return jsonify(fixture="ui-preview", external_services=False)


@app.route("/logout", endpoint="auth.logout")
def logout():
    return render_template("auth.html")


@app.route("/dashboard", endpoint="admin.dashboard")
def dashboard():
    return render_template("dashboard.html", user=USER)


@app.route("/settings", endpoint="settings.settings_page")
def settings_page():
    return render_template("settings.html", user=USER, origin="chat")


@app.route("/offline", endpoint="pwa.offline")
def offline():
    return render_template("offline.html")


@app.route("/manifest.webmanifest", endpoint="pwa.manifest")
def manifest():
    return jsonify({"name": "SC Assistant", "start_url": "/", "display": "standalone"})


@app.route("/sw.js", endpoint="pwa.service_worker")
def service_worker():
    # Empty worker: a real one would cache these very files and hide edits.
    return Response("/* preview: no-op */", mimetype="application/javascript")


# ─────────────────────────────────────────────────────────────────────────────
# JSON the page fetches on load.
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/chat/conversations")
def conversations():
    return jsonify([
        {"conv_id": "c1", "title": "Enrollment requirements for transferees", "created_at": "2026-09-03T08:00:00"},
        {"conv_id": "c2", "title": "Graduate studies programs offered", "created_at": "2026-09-02T08:00:00"},
        {"conv_id": "c3", "title": "Tuition payment deadlines", "created_at": "2026-09-01T08:00:00"},
    ])


ANSWER = (
    "## Enrollment for transferees\n\n"
    "Transferees are admitted through the **Office of the Registrar**. "
    "You will need the documents below before an evaluation can start.\n\n"
    "1. **Transcript of Records** (or a certified copy of grades)\n"
    "2. **Honorable Dismissal** from your previous school\n"
    "3. Certificate of Good Moral Character\n"
    "4. Two 2x2 ID photos\n\n"
    "> Evaluation usually takes two working days.\n\n"
    "| Step | Office | Fee |\n"
    "|------|--------|-----|\n"
    "| Credential evaluation | Registrar | Free |\n"
    "| Entrance assessment | Guidance | PHP 150 |\n"
    "| Enrollment proper | Cashier | Varies |\n\n"
    "For the complete policy, see the student handbook.[SOURCE: Samar-College-2024.pdf | 26]\n"
)

HISTORY = [
    {"role": "user", "content": "What are the requirements for transferees?"},
    {"role": "assistant", "content": ANSWER, "msg_id": "m1", "citations": [{"label": "Samar-College-2024.pdf|p.26"}]},
    {"role": "user", "content": "And how long does the evaluation take?"},
    {"role": "assistant",
     "content": "Evaluation of credentials normally takes **two working days** once the "
                "Registrar has a complete set of documents.[SOURCE: Samar-College-2024.pdf | 27]",
     "msg_id": "m2", "citations": [{"label": "Samar-College-2024.pdf|p.27"}]},
]


@app.get("/chat/conversation/<cid>")
def conversation(cid):
    return jsonify({"messages": HISTORY})


@app.post("/chat/conversation/<cid>/restore")
def restore(cid):
    return jsonify({"success": True})


@app.delete("/chat/conversation/<cid>/delete")
def delete_conv(cid):
    return jsonify({"success": True})


@app.post("/chat/clear")
def clear():
    return jsonify({"success": True})


@app.get("/chat/api/suggestions")
def chat_suggestions():
    return jsonify({"success": True, "suggestions": [
        "How do I enroll?",
        "What are the tuition fees and payment options?",
        "What scholarships are available?",
    ]})


@app.get("/api/announcements")
def announcements():
    # Legacy data stays populated so UI tests catch any accidental banner polling.
    return jsonify({
        "success": True,
        "viewer": "student",
        "announcements": [{
            "key": "a1",
            "priority": "info",
            "title": "Prepare your enrollment documents",
            "body": "Check the required documents before visiting the Registrar's Office.",
            "expires_on": None,
        }],
    })


@app.get("/chat/escalate/routes")
def routes():
    return jsonify({"success": True, "routes": {"admin": "College Administration"}})


@app.post("/chat/escalate")
def escalate():
    return jsonify({"success": True, "message": "Preview question sent to staff."})


@app.post("/chat/report")
def report_answer():
    return jsonify({"status": "ok", "success": True})


@app.get("/chat/escalations/mine")
def mine():
    return jsonify({"success": True, "escalations": []})


@app.post("/chat/feedback")
def feedback():
    return jsonify({"success": True, "can_report": True})


@app.post("/chat/feedback/batch")
def feedback_batch():
    return jsonify({"success": True, "votes": {"m1": "up"}})


@app.post("/chat/get")
def chat_get():
    """The streaming answer, so the typing indicator and the token-by-token
    growth of a bubble can both be reviewed — neither is visible in a snapshot
    of a finished page."""
    def gen():
        for label in ("Searching the handbook…", "Reading 34 pages…", "Writing the answer…"):
            yield f"data: {json.dumps({'type': 'phase', 'label': label})}\n\n"
            time.sleep(0.4)
        for i in range(0, len(ANSWER), 24):
            yield f"data: {json.dumps({'type': 'chunk', 'text': ANSWER[i:i + 24]})}\n\n"
            time.sleep(0.03)
        yield "data: " + json.dumps({
            "type": "done", "msg_id": "m9",
            "citations": [{"label": "Samar-College-2024.pdf|p.26"}],
            "new_conversation_created": False, "conv_id": "c1",
        }) + "\n\n"
    return Response(gen(), mimetype="text/event-stream")


# Dashboard JSON, so the admin screen is not a wall of spinners.
@app.get("/api/dashboard/attention")
def attention():
    return jsonify({"success": True, "attention": [
        {"section": "escalations", "count": 3, "label": "Students waiting for a reply", "icon": "fa-user-tie", "tone": "warn"},
        {"section": "reports", "count": 2, "label": "Answers reported for review", "icon": "fa-flag", "tone": "bad"},
    ]})


@app.get("/api/dashboard/activities")
def activities():
    return jsonify({"success": True, "window_days": 30, "activities": [
        {"at": "2026-11-13T05:00:00Z", "text": "Signed in: <strong>Maria Santos</strong> (maria.santos@example.invalid)",
         "time": "2 minutes ago", "icon": "fa-right-to-bracket", "tone": "info", "section": "users"},
        {"kind": "upload", "text": "Samar-College-update.pdf indexed", "time": "2 hours ago", "icon": "fa-file", "tone": "ok"},
        {"kind": "feedback", "text": "An answer was marked unhelpful", "time": "5 hours ago", "icon": "fa-comment", "tone": "bad"},
    ]})


@app.get("/api/dashboard/users")
def users():
    return jsonify({"success": True, "users": [
        {"id": "preview-juan", "username": "Juan Dela Cruz", "email": "juan@samarcollege.edu.ph",
         "student_id": "00012345", "role": "admin", "status": "CONFIRMED", "joined": "2026-08-01T08:00:00Z"},
        {"id": "preview-maria", "username": "Maria Santos", "email": "maria@samarcollege.edu.ph",
         "student_id": "", "role": "user", "status": "UNCONFIRMED", "joined": "2026-09-01T08:00:00Z"},
    ]})


@app.post("/api/dashboard/users/update")
def update_user():
    return jsonify(success=True, message="Preview user updated.", is_self_update=False,
                   new_username=request.form.get("username", ""))


# Fixed admin snapshots keep layout checks independent of the wall clock and chat history.
ADMIN_PREVIEW_TODAY = "2026-11-11"
REPORT_HISTORY = [
    {"role": "user", "content": (
        "I am transferring from another college and will be travelling with my parent to submit "
        "my records. Could you explain the enrollment requirements, which office evaluates my "
        "previous subjects, and whether I can also ask about scholarship assistance during the "
        "same visit? I need to plan around work and the time needed to request my transcript."
    )},
    {"role": "assistant", "content": (
        "Start with a credential evaluation at the Office of the Registrar before choosing "
        "subjects or paying enrollment fees. Bring your available academic records and a list "
        "of the subjects you completed so the evaluator can identify missing documents.\n\n"
        "Scholarship applications are reviewed separately. Ask Student Affairs which supporting "
        "documents apply to your circumstances and whether their submission deadline differs "
        "from the enrollment period. An evaluation is not a guarantee of admission or funding."
    )},
    {"role": "user", "content": (
        "My previous school can issue a certified copy of grades now, but the official transcript "
        "may arrive later. Please give me a detailed checklist that I can show my parent, including "
        "the evaluation dates, the offices to visit, and what needs confirmation before we travel."
    )},
    {"role": "assistant", "content": (
        "The transferee evaluation window runs from November 2 to November 20, 2026. Submit your "
        "available records to the Registrar and explain which documents are still being processed "
        "by your previous school. Ask the evaluator to confirm in writing whether a certified "
        "copy of grades is acceptable while you wait for the official transcript.\n\n"
        "### Documents and office checklist\n\n"
        "| Document or step | Where to submit | What to check before travelling |\n"
        "| --- | --- | --- |\n"
        "| Transcript of Records or certified copy of grades | Office of the Registrar | "
        "Confirm whether a temporary copy can begin the evaluation and when the original is due. |\n"
        "| Honorable Dismissal and Good Moral Certificate | Admissions and Records counter | "
        "Request original signed documents from your previous school and keep copies for reference. |\n"
        "| Scholarship application and supporting documents | Student Affairs Office | "
        "Ask about eligibility, the separate application deadline, and any additional evidence. |\n"
        "| Written subject-credit evaluation | Program coordinator and Registrar | "
        "Check the approved subjects before selecting a class schedule or paying enrollment fees. |\n\n"
        "Allow **two working days after a complete submission** for the initial evaluation. "
        "Missing records or a program-specific review can extend that estimate, so do not make "
        "non-refundable travel arrangements based only on this answer.\n\n"
        "Bring a copy of `SamarCollegeTransfereeEnrollmentRequirementsAndScholarshipApplication"
        "GuidelinesForAcademicYear20262027.pdf` if you have been given that checklist, along with "
        "your application reference and contact details.\n\n"
        "> Confirm the dates and any temporary-document arrangements directly with the Registrar. "
        "A scholarship review does not automatically reserve an enrollment slot."
    )},
    {"role": "user", "content": (
        "Can I request a written evaluation before paying any enrollment fees? I would like to "
        "review the credited subjects with my parent before we commit to a class schedule."
    )},
    {"role": "assistant", "content": (
        "You can request a written evaluation before making a payment. Ask the Registrar which "
        "subjects have been credited, which remain under review, and whether the proposed class "
        "schedule has been approved by your program coordinator. Keep the written response with "
        "your application records and confirm the payment instructions with the Cashier."
    )},
]


@app.get("/admin/reports/api/list")
def admin_reports():
    reports = [
        {"report_id": "mobile-report-1", "status": "pending", "created_at": "2026-11-11T08:40:00Z",
         "reporter_email": "previewstudentwithaverylongenrollmentandscholarshipinquiry@students.preview.example",
         "reporter_uid": "preview-student", "conv_id": "preview-conversation", "reason": "Others",
         "other_text": (
             "The answer combines enrollment and scholarship deadlines, but the printed notice "
             "from my previous visit lists a different closing date. Please verify whether the "
             "temporary copy of grades is sufficient and clarify which deadline applies before "
             "my parent takes time off work and pays for travel to the campus."
         ),
         "msg_snippet": REPORT_HISTORY[3]["content"][:240]},
        {"report_id": "mobile-report-2", "status": "pending", "created_at": "2026-11-10T04:20:00Z",
         "reporter_email": "previewstudentwithaverylongenrollmentandscholarshipinquiry@students.preview.example",
         "reporter_uid": "preview-student", "conv_id": "preview-conversation",
         "reason": "Incorrect Information", "other_text": "", "msg_snippet": REPORT_HISTORY[1]["content"][:240]},
        {"report_id": "mobile-report-3", "status": "done", "created_at": "2026-11-09T02:15:00Z",
         "reporter_email": "previewstudentwithaverylongenrollmentandscholarshipinquiry@students.preview.example",
         "reporter_uid": "preview-student", "conv_id": "preview-conversation",
         "reason": "Incomplete Answer", "other_text": "", "msg_snippet": REPORT_HISTORY[5]["content"][:240]},
    ]
    status = request.args.get("status")
    return jsonify([report for report in reports if report["status"] == status] if status else reports)


@app.get("/admin/reports/api/conversation/<uid>/<conv_id>")
def admin_report_conversation(uid, conv_id):
    if (uid, conv_id) != ("preview-student", "preview-conversation"):
        return jsonify({"messages": []})
    return jsonify({"messages": REPORT_HISTORY})


@app.get("/admin/calendar/api/list")
def admin_calendar():
    return jsonify({"success": True, "state": {
        "today": ADMIN_PREVIEW_TODAY,
        "active": [{
            "key": "mobile-calendar-active",
            "label": "Transferee enrollment, document evaluation, and scholarship application assistance",
            "start": "2026-11-02", "end": "2026-11-20", "days_left": 9,
            "note": (
                "Bring original records and photocopies, allow extra time for subject-credit "
                "evaluation, and confirm any missing-document arrangements with the Registrar "
                "before proceeding to the Cashier or selecting a class schedule."
            ),
            "set_by": "previewregistrarandenrollmentcoordination@staff.preview.example",
        }],
        "upcoming": [{
            "key": "mobile-calendar-upcoming",
            "label": "Final examination consultation and clearance review for graduating students",
            "start": "2026-12-01", "end": "2026-12-05", "days_until_start": 20,
            "note": "Consult your program coordinator about outstanding requirements before requesting clearance.",
            "set_by": "previewacademiccoordination@staff.preview.example",
        }],
        "past": [{
            "key": "mobile-calendar-past", "label": "Midterm assessment and make-up examination requests",
            "start": "2026-10-26", "end": "2026-10-30", "days_since_end": 12,
            "note": "The regular request window has ended; exceptional cases need the program coordinator's review.",
            "set_by": "previewacademiccoordination@staff.preview.example",
        }],
    }, "stats": {"active": 1, "upcoming": 1, "past": 1}})


@app.get("/admin/announcements/api/list")
def admin_announcements():
    return jsonify({"success": True, "today": ADMIN_PREVIEW_TODAY, "announcements": [
        {
            "key": "mobile-announcement-live", "priority": "urgent",
            "title": "Enrollment advisory for transferees and returning students submitting documents during the extended evaluation window",
            "body": (
                "Students travelling from outside the city should confirm the status of their "
                "credential evaluation before booking transport. Bring the original documents "
                "and a complete set of photocopies, and keep the acknowledgement from your "
                "previous school if an official transcript is still being processed. Scholarship "
                "applications have separate requirements and do not automatically reserve an "
                "enrollment slot. For this preview notice, the coordination contact is "
                "previewenrollmentandscholarshipcoordination@students.preview.example."
            ),
            "audience": "all", "starts_on": "2026-11-09", "expires_on": "2026-11-20",
            "active": True, "live": True, "expired": False, "pinned": True,
            "posted_by": "previewstudentaffairsandcommunications@staff.preview.example",
        },
        {
            "key": "mobile-announcement-expired", "priority": "info",
            "title": "Library orientation and borrowing-policy briefing has concluded",
            "body": "The orientation sessions are finished. Students who could not attend may request the briefing notes at the library desk.",
            "audience": "students", "starts_on": "2026-10-19", "expires_on": "2026-10-23",
            "active": True, "live": False, "expired": True, "pinned": False,
            "posted_by": "previewlibraryservices@staff.preview.example",
        },
    ], "stats": {"active": 1, "urgent": 1, "expired": 1}})


@app.get("/files")
def files():
    return jsonify({"success": True, "files": [
        {"name": "Samar-College-2024.pdf", "size": 2411002, "uploaded_at": "2026-08-01T08:00:00Z"},
        {"name": "Samar-College-update.pdf", "size": 881233, "uploaded_at": "2026-09-01T08:00:00Z"},
        {"name": "SamarCollegeTransfereeEnrollmentRequirementsAndScholarshipApplicationGuidelinesForAcademicYear20262027.pdf",
         "size": 3187456, "uploaded_at": "2026-11-11T08:30:00Z"},
    ]})


@app.get("/admin/analytics/api/overview")
def analytics_overview():
    """
    The analytics screen, which is the one dashboard section the catch-all below
    could not fake.

    Worth explaining, because it is the only route here that exists to satisfy a
    payload SHAPE rather than to make a screen look populated. The catch-all
    answers every unknown path with `{"success": true}`, and dashboard.js reads
    `res.analytics.window_days` straight off that — `success` is true, so it never
    takes the error branch, and it dereferences a key that is not there. The page
    threw "Cannot read properties of undefined (reading 'window_days')" on every
    single load and the whole analytics card stayed empty.

    That is a harness bug and not a product bug: the real endpoint in
    admin_analytics.py returns {"success": True, "analytics": overview(days)}.
    But it mattered anyway, because a live console error is the first thing you
    look at when reviewing a page, and this one was drowning out anything real.

    The keys below are copied from rag.analytics.overview()'s return statement
    rather than invented to fit the chart. My first attempt at this stub guessed
    the shape from what the top of renderAnalytics reads, the page then threw on
    `response_times.pending_count` twelve lines further down, and I would have
    kept discovering one key per reload. Reading the producer once is what ends
    that loop — and it is also the only way the numbers on screen stay plausible
    against each other, since `answered + unanswered` has to equal the questions
    total or the KPI row contradicts the chart beside it.
    """
    series = [
        {
            "date": f"2026-08-{d:02d}",
            "answered": 8 + (d % 7) * 3,
            "unanswered": (d % 4),
            "escalated": 1 if d % 6 == 0 else 0,
            "resolved": 1 if d % 7 == 0 else 0,
            "total": 8 + (d % 7) * 3 + (d % 4),
            "answered_rate": round((8 + (d % 7) * 3) / (8 + (d % 7) * 3 + (d % 4)), 3),
        }
        for d in range(4, 32)
    ]
    hourly = [
        {
            "hour": h,
            "label": f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}",
            "count": [0, 0, 0, 0, 0, 1, 3, 8, 17, 24, 31, 28,
                      19, 22, 27, 25, 18, 11, 7, 5, 3, 2, 1, 0][h],
            "share_of_peak": round([0, 0, 0, 0, 0, 1, 3, 8, 17, 24, 31, 28,
                                    19, 22, 27, 25, 18, 11, 7, 5, 3, 2, 1, 0][h] / 31, 3),
        }
        for h in range(24)
    ]
    topics = [
        {"key": "enrollment", "topic": "Admissions and enrollment", "total": 64},
        {"key": "fees", "topic": "Tuition and payments", "total": 41},
        {"key": "scholarships", "topic": "Scholarships", "total": 22},
    ]
    return jsonify({"success": True, "analytics": {
        "generated_at": "2026-09-03T09:00:00+00:00",
        "window_days": request.args.get("days", 14, type=int),
        "totals": {
            "conversations": 128,
            "questions_with_signal": 412, "answered": 366, "unanswered": 46,
            "answered_rate": 0.888, "votes": 132, "up": 108, "down": 24,
            "satisfaction": 0.818, "escalated": 12, "escalations_resolved": 9,
        },
        "series": series,
        "hourly": hourly,
        "peak_hour": "10 AM",
        "top_topics": topics,
        "topic_usage_available": True,
        "unanswered_topics": [topics[2]],
        "suspect_documents": [
            {"source": "Samar-College-2024.pdf", "up": 12, "down": 7, "total": 19},
            {"source": "Samar-College-update.pdf", "up": 30, "down": 2, "total": 32},
        ],
        "response_times": {
            "answered_count": 9, "avg_hours": 6.4, "median_hours": 3.1,
            "pending_count": 3, "oldest_pending_hours": 27.5,
        },
        "trend": {
            "recent": {"questions": 212, "answered": 191, "unanswered": 21,
                       "answered_rate": 0.901, "satisfaction": 0.84},
            "prior": {"questions": 200, "answered": 175, "unanswered": 25,
                      "answered_rate": 0.875, "satisfaction": 0.79},
            "delta": {"questions": 12, "answered_rate": 0.026, "satisfaction": 0.05},
        },
    }})


# Anything else the page pokes at: answer 200 with an empty envelope rather than
# letting a 404 fill the console and hide the errors that matter.
#
# Note the trade-off this makes, since it bit me above: a blanket `success: true`
# keeps the console clean but it also means any front-end that reads a nested key
# off the response gets `undefined` instead of a failure it can handle. When that
# happens the fix is a real stub above this line, not a change here — narrowing
# the catch-all would just replace one misleading console error with another.
@app.route("/<path:_ignored>", methods=["GET", "POST", "PUT", "DELETE"])
def catch_all(_ignored):
    return jsonify({"success": True})


if __name__ == "__main__":
    print("\n  UI preview (templates + CSS only, no AWS):")
    print("    login      http://localhost:5055/login")
    print("    chat       http://localhost:5055/")
    print("    chat/guest http://localhost:5055/guest-chat")
    print("    dashboard  http://localhost:5055/dashboard")
    print("    settings   http://localhost:5055/settings")
    print("    offline    http://localhost:5055/offline\n")
    app.run(host="127.0.0.1", port=5055, debug=False, threaded=True)
