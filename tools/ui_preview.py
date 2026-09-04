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
look like a page: a conversation list with entries in it, an announcement banner
with a notice in it, an answer that streams in token by token. Those are the
states worth reviewing, and they are exactly the states you never see on an
empty dev database.
"""

import json
import os
import sys
import time

# The harness lives in tools/, the app lives one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response, jsonify, render_template  # noqa: E402
from jinja2 import ChainableUndefined  # noqa: E402

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


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints the templates name in url_for(). The names matter, the paths do not.
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/chat", endpoint="chat.chat_page")
def index():
    return render_template("chat.html", user=USER, start_new="true")


app.add_url_rule("/", endpoint="chat.index", view_func=index)


@app.route("/guest-chat")
def guest_chat():
    return render_template("chat.html", user=GUEST, start_new="true")


@app.route("/login", endpoint="auth.auth_page")
def login():
    return render_template("auth.html")


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


@app.get("/api/announcements")
def announcements():
    return jsonify({
        "viewer": "student",
        "announcements": [{
            "key": "a1",
            "kind": "important",
            "title": "Enrollment for 2nd Semester is ongoing",
            "body": "Walk-in enrollment runs until 12 September at the Registrar's Office.",
            "until": "Until 12 September 2026",
        }],
    })


@app.get("/chat/escalate/routes")
def routes():
    return jsonify({"success": True, "routes": [{"key": "admin", "label": "College Administration"}]})


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
    return jsonify({"success": True, "items": [
        {"kind": "escalations", "count": 3, "label": "students waiting for a reply"},
        {"kind": "reports", "count": 2, "label": "reports filed by hand"},
    ]})


@app.get("/api/dashboard/activities")
def activities():
    return jsonify({"success": True, "activities": [
        {"kind": "upload", "text": "Samar-College-update.pdf indexed", "when": "2 hours ago"},
        {"kind": "feedback", "text": "An answer was marked unhelpful", "when": "5 hours ago"},
    ]})


@app.get("/api/dashboard/users")
def users():
    return jsonify({"success": True, "users": [
        {"username": "Juan Dela Cruz", "email": "juan@samarcollege.edu.ph", "role": "admin"},
        {"username": "Maria Santos", "email": "maria@samarcollege.edu.ph", "role": "student"},
    ]})


@app.get("/files")
def files():
    return jsonify({"success": True, "files": [
        {"name": "Samar-College-2024.pdf", "size": 2411002, "last_modified": "2026-08-01"},
        {"name": "Samar-College-update.pdf", "size": 881233, "last_modified": "2026-09-01"},
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
        {"key": "enrollment", "topic": "Enrollment requirements", "total": 64,
         "answered": 61, "unanswered": 3, "escalated": 1, "up": 44, "down": 3,
         "last_asked": "2026-09-03"},
        {"key": "tuition", "topic": "Tuition and payment deadlines", "total": 41,
         "answered": 38, "unanswered": 3, "escalated": 2, "up": 25, "down": 6,
         "last_asked": "2026-09-02"},
        {"key": "scholarship", "topic": "Scholarship deadlines", "total": 22,
         "answered": 9, "unanswered": 13, "escalated": 5, "up": 6, "down": 8,
         "last_asked": "2026-09-01"},
    ]
    return jsonify({"success": True, "analytics": {
        "generated_at": "2026-09-03T09:00:00+00:00",
        "window_days": 30,
        "totals": {
            "questions_with_signal": 412, "answered": 366, "unanswered": 46,
            "answered_rate": 0.888, "votes": 132, "up": 108, "down": 24,
            "satisfaction": 0.818, "escalated": 12, "escalations_resolved": 9,
        },
        "series": series,
        "hourly": hourly,
        "peak_hour": "10 AM",
        "top_topics": topics,
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
