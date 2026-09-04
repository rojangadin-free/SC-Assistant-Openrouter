"""
test_activity.py — the accurate activity feed and the triage counts.

    python tests/test_activity.py

The bug this suite exists for
----------------------------
The Overview screen's "Recent Activities" list was built from
`files_table.scan(Limit=5)`. `Limit` on a DynamoDB scan does not mean "the newest
5" — a scan has no order, so it returned five arbitrary rows and then sorted
*those* by timestamp. The list looked plausible and was wrong: a document
uploaded a minute ago appeared only if it happened to be among the first five rows
the scan walked past.

The important assertion in here is therefore not "the feed renders" but **"the
newest event is actually first, out of more events than the feed shows"** — which
is exactly what the old implementation could not do and what no amount of sorting
five random rows would have fixed.

Every store is pointed at a throwaway temp file before the modules are imported,
so this runs offline with no AWS and cannot touch real data.
"""

import _bootstrap  # noqa: F401

import datetime
import os
import tempfile

# Local JSON files, not DynamoDB, and each one disposable. Set before importing
# any rag module: the stores read these names when they build their handles.
os.environ["STORE_BACKEND"] = "file"
_TMP = tempfile.mkdtemp(prefix="sc_activity_test_")

_FILES = {
    "ANSWER_FEEDBACK_FILE": "answer_feedback.json",
    "CONTENT_GAPS_FILE": "content_gaps.json",
    "ESCALATIONS_FILE": "escalations.json",
    "CONFLICT_RESOLUTIONS_FILE": "conflict_resolutions.json",
    "CALENDAR_PERIODS_FILE": "calendar_periods.json",
    "DOC_FRESHNESS_FILE": "doc_freshness.json",
}
for _env, _name in _FILES.items():
    os.environ[_env] = os.path.join(_TMP, _name)

from rag import activity  # noqa: E402
from rag import feedback, gaps, escalation, conflicts, calendar  # noqa: E402

_passed = 0
_failed = 0


def check(label, condition):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}")


def section(title):
    print(f"\n=== {title} ===")


def _iso(minutes_ago):
    """A UTC timestamp shaped exactly like the ones the real stores write."""
    dt = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(minutes=minutes_ago))
    return dt.isoformat()


def _backdate_vote(msg_id, minutes_ago):
    """
    Move a stored vote's timestamp. The feed's whole job is ordering by time, so
    the only way to test it is to control the times.
    """
    data = feedback._load()
    data["votes"][msg_id]["at"] = _iso(minutes_ago)
    feedback._save(data)


# --------------------------------------------------------------------------- #
section("1. Timestamps are read in Manila time and read naturally")
# --------------------------------------------------------------------------- #

check("a moment ago -> 'just now'", activity.humanize(_iso(0)) == "just now")
check("40 minutes -> '40 minutes ago'",
      activity.humanize(_iso(40)) == "40 minutes ago")
check("singular minute has no 's'",
      activity.humanize(_iso(1)) == "1 minute ago")
check("3 hours -> '3 hours ago'",
      activity.humanize(_iso(180)) == "3 hours ago")
check("singular hour has no 's'",
      activity.humanize(_iso(61)) == "1 hour ago")
check("30 hours -> 'yesterday'", activity.humanize(_iso(60 * 30)) == "yesterday")
check("4 days -> '4 days ago'",
      activity.humanize(_iso(60 * 24 * 4)) == "4 days ago")

# Past a week, an absolute date. "23 days ago" is arithmetic the reader has to do.
old = activity.humanize(_iso(60 * 24 * 30))
check("a month ago becomes a real date", "ago" not in old and old != "")
# %-d is a Linux-ism that throws on Windows, and this project is developed on
# Windows and deployed on Linux — the kind of break that only shows up in prod.
check("no %-d leaking on Windows", "-" not in old)

# A malformed timestamp must yield no label rather than an exception; one bad row
# in a store must not blank the feed.
check("garbage timestamp -> empty string", activity.humanize("not-a-date") == "")
check("empty timestamp -> empty string", activity.humanize("") == "")
check("None is survivable", activity.humanize(None) == "")


# --------------------------------------------------------------------------- #
section("2. THE BUG: the newest event really is first")
# --------------------------------------------------------------------------- #

# Twelve events at known, different times, recorded out of order on purpose. The
# feed shows ten, so the sort has to be a real sort over everything — which is
# precisely what scan(Limit=5) could not do.
feedback.record_vote("m-oldest", "up", question="Oldest question", conv_id="c1")
_backdate_vote("m-oldest", 60 * 24 * 6)                    # 6 days ago

for i, mins in enumerate([500, 400, 300, 200, 100, 90, 80, 70, 60, 50]):
    feedback.record_vote(f"m{i}", "up", question=f"Question {i}", conv_id="c1")
    _backdate_vote(f"m{i}", mins)

# The one that must come first, recorded LAST and only one minute old.
feedback.record_vote("m-newest", "down", question="Newest question", conv_id="c1")
_backdate_vote("m-newest", 1)

rows = activity.feed(10)
check("the feed is capped at the requested length", len(rows) == 10)
check("the NEWEST event is first", "Newest question" in rows[0]["text"])
check("...and it is labelled as recent", rows[0]["time"] == "1 minute ago")

# The real regression test: an event older than the cap must be pushed out, not
# retained because it happened to be scanned first.
_texts = " ".join(r["text"] for r in rows)
check("an older event is pushed out by newer ones", "Oldest question" not in _texts)

# Strictly descending, across the whole list.
_stamps = [r["at"] for r in rows]
check("every row is in newest-first order", _stamps == sorted(_stamps, reverse=True))


# --------------------------------------------------------------------------- #
section("3. Rows carry what the UI needs to be useful")
# --------------------------------------------------------------------------- #

row = rows[0]
for field in ("at", "time", "icon", "tone", "text", "section"):
    check(f"row has '{field}'", field in row)

# `section` is what makes a row clickable. A feed that reports a problem without
# offering the screen that fixes it just makes the admin hunt for it.
check("a feedback row points at the feedback section", row["section"] == "feedback")
# A downvote must not look like good news.
check("a downvote is toned as bad", row["tone"] == "bad")
check("an upvote is toned as ok",
      any(r["tone"] == "ok" for r in rows if "Question" in r["text"]))
# jsonify cannot serialise a datetime; the internal sort key must be gone.
check("the internal sort key is not leaked", "_sort" not in row)


# --------------------------------------------------------------------------- #
section("4. Every store reaches the feed")
# --------------------------------------------------------------------------- #

gaps.record_gap("What is the shuttle schedule?", conv_id="c2")
escalation.create_escalation("Can I enroll late?", route="admin",
                             student_email="s@example.com")
conflicts.set_resolution("dean|education", "Jane Cruz",
                         role="dean", subject="education",
                         resolved_by="admin@example.com")
calendar.set_period("Enrollment for 1st Semester", "2026-08-01", "2026-08-15",
                    set_by="admin@example.com")


everything = activity.feed(50)
_all = " ".join(r["text"] for r in everything)
_sections = {r["section"] for r in everything}

check("an unanswered question appears", "shuttle" in _all.lower())
check("...tagged for the gaps screen", "gaps" in _sections)
check("an escalation appears", "enroll late" in _all.lower())
check("...tagged for the escalations screen", "escalations" in _sections)
check("a resolved conflict appears", "Jane Cruz" in _all)
check("...tagged for the conflicts screen", "conflicts" in _sections)
check("a calendar change appears", "Enrollment" in _all)
check("...tagged for the calendar screen", "calendar" in _sections)

# The escalation reply is its own event at its own time — collapsing the two would
# lose the only thing worth knowing, which is how long the student waited.
_esc = escalation.list_escalations()[0]
escalation.answer_escalation(_esc["id"], "Yes, until Friday.",
                             answered_by="registrar@example.com")
after = activity.feed(50)
check("a reply is its own event", any("replied" in r["text"] for r in after))
check("...and names who replied",
      any("registrar@example.com" in r["text"] for r in after))


# --------------------------------------------------------------------------- #
section("5. Text is safe to drop into a row")
# --------------------------------------------------------------------------- #

feedback.record_vote("m-multiline", "up",
                     question="Line one\nline two\n\nline three", conv_id="c1")
target = next((r for r in activity.feed(50) if "Line one" in r["text"]), None)
check("a multi-line question is found", target is not None)
# A raw newline from a textarea breaks the row it sits in.
check("newlines are flattened", "\n" not in (target or {}).get("text", ""))

feedback.record_vote("m-long", "up", question="word " * 80, conv_id="c1")
check("a very long question does not produce a very long row",
      all(len(r["text"]) < 220 for r in activity.feed(50)))


# --------------------------------------------------------------------------- #
section("6. Rows from DynamoDB are merged, not appended")
# --------------------------------------------------------------------------- #

# Uploads and new accounts live in DynamoDB/Cognito, which only admin.py can
# reach, so it passes them in. If they were appended rather than merged into the
# sort, a document uploaded a minute ago would sit below a week-old vote — the
# original bug wearing a different hat.
merged = activity.feed(10, extra=[{
    "at": _iso(0),
    "icon": "fa-file-arrow-up",
    "text": "New document uploaded: <strong>handbook.pdf</strong>",
    "section": "uploads",
}])
check("an injected row can outrank everything stored",
      "handbook.pdf" in merged[0]["text"])
check("...and is given a human time", merged[0]["time"] == "just now")
check("...and defaults to a tone", merged[0]["tone"] == "info")

# An injected row with no usable timestamp cannot be placed on a timeline, so it
# is dropped rather than silently sorted to one end.
dropped = activity.feed(10, extra=[{"at": "", "text": "no timestamp"}])
check("an undateable injected row is dropped",
      not any("no timestamp" in r["text"] for r in dropped))


# --------------------------------------------------------------------------- #
section("7. Old events fall out of the window")
# --------------------------------------------------------------------------- #

feedback.record_vote("m-ancient", "up", question="Ancient history", conv_id="c1")
_backdate_vote("m-ancient", 60 * 24 * 200)                 # 200 days ago

check("a 200-day-old event is outside a 30-day window",
      not any("Ancient history" in r["text"]
              for r in activity.feed(50, window_days=30)))
check("...and inside a 365-day window",
      any("Ancient history" in r["text"]
          for r in activity.feed(100, window_days=365)))


# --------------------------------------------------------------------------- #
section("8. Triage: what is waiting for a human")
# --------------------------------------------------------------------------- #

rows = activity.attention()
check("the panel has rows", len(rows) >= 3)
for r in rows:
    check(f"'{r['section']}' row is shaped for the UI",
          all(k in r for k in ("count", "label", "section", "icon", "tone")))

_by_section = {r["section"]: r for r in rows}
check("escalations are counted", "escalations" in _by_section)
check("content gaps are counted", "gaps" in _by_section)
check("unhelpful answers are counted", "feedback" in _by_section)

# A person waiting on a reply is the most urgent thing on the screen, so it leads.
check("waiting students come first", rows[0]["section"] == "escalations")

# The only escalation was answered above, so nothing should be pending now.
check("an answered escalation is no longer pending",
      _by_section["escalations"]["count"] == 0)

# Grammar, because "1 students are waiting" is the kind of detail that makes a
# dashboard look unfinished.
escalation.create_escalation("One more?", route="admin",
                             student_email="s2@example.com")
one = {r["section"]: r for r in activity.attention()}["escalations"]
check("a single item reads as singular",
      one["count"] == 1 and one["label"].startswith("student is"))

escalation.create_escalation("And another?", route="admin",
                             student_email="s3@example.com")
two = {r["section"]: r for r in activity.attention()}["escalations"]
check("two items read as plural",
      two["count"] == 2 and two["label"].startswith("students are"))

# Unresolved conflicts are deliberately NOT here: finding them means re-parsing
# every source PDF, which is far too expensive for a screen that loads on every
# visit and refreshes on a timer.
check("conflicts are not counted on the landing screen",
      "conflicts" not in _by_section)


# --------------------------------------------------------------------------- #
section("9. Health numbers do not invent good or bad news")
# --------------------------------------------------------------------------- #

h = activity.health()
for field in ("votes", "satisfaction", "answered", "unanswered"):
    check(f"health has '{field}'", field in h)
check("votes are counted", h["votes"] > 0)
check("satisfaction is a fraction, not a percentage",
      h["satisfaction"] is None or 0.0 <= h["satisfaction"] <= 1.0)
check("unanswered questions are counted", h["unanswered"] >= 1)

# The important one. With no votes at all, satisfaction must be None — reporting
# 0% because there is no data is a lie in the most damaging direction, and it is
# the same reason analytics._rate returns None instead of 0.0.
feedback._save({"votes": {}, "topics": {}, "updated_at": ""})
empty = activity.health()
check("no votes -> satisfaction is None, not 0%", empty["satisfaction"] is None)
check("...and the vote count is a real zero", empty["votes"] == 0)


# --------------------------------------------------------------------------- #
section("10. A broken store costs its own rows, not the dashboard")
# --------------------------------------------------------------------------- #

# A reporting surface must never be able to take down the screen it reports on,
# let alone the assistant.
with open(os.environ["CONTENT_GAPS_FILE"], "w", encoding="utf-8") as fh:
    fh.write("{ this is not json")

try:
    survived = activity.feed(10)
    check("a corrupt store does not raise", True)
    check("...and the other stores still report", isinstance(survived, list))
except Exception as e:
    check(f"a corrupt store does not raise (raised {e})", False)

try:
    activity.attention()
    check("triage survives a corrupt store", True)
except Exception as e:
    check(f"triage survives a corrupt store (raised {e})", False)


# --------------------------------------------------------------------------- #
section("11. An empty system says so")
# --------------------------------------------------------------------------- #

for _env in _FILES:
    path = os.environ[_env]
    if os.path.exists(path):
        os.remove(path)

check("no data -> an empty feed, not an error", activity.feed(10) == [])
# The rows still exist at zero: an empty panel that says "nothing is waiting" is
# a stronger statement than a panel that silently disappeared.
zeroed = activity.attention()
check("triage still returns its rows", len(zeroed) >= 3)
check("...all at zero", all(r["count"] == 0 for r in zeroed))


# --------------------------------------------------------------------------- #
section("12. The endpoints the dashboard landing screen actually calls")

# --------------------------------------------------------------------------- #
#
# Everything above tests the module. These two routes are what the browser hits,
# and they are where the DynamoDB merge and the admin check live — neither of
# which the module can be asked about.
#
# The heavy imports are stubbed the same way test_analytics.py does it: importing
# sc_assistant pulls in Pinecone, boto3 and an embedding model, none of which
# belong in a test of a JSON feed.
import sys
import types

_fake_chain = types.ModuleType("rag.chain")
_fake_chain.app_graph = types.SimpleNamespace(
    invoke=lambda *a, **k: {}, update_state=lambda *a, **k: None,
    get_state=lambda *a, **k: None)
_fake_chain.chatModel = types.SimpleNamespace(stream=lambda *a, **k: iter(()))
_fake_chain.stream_answer = lambda *a, **k: iter(())
_fake_chain.embeddings = None
sys.modules["rag.chain"] = _fake_chain

_fake_s3 = types.ModuleType("aws.s3")
for _n in ("get_s3_presigned_url", "upload_file_to_s3", "delete_file_from_s3"):
    setattr(_fake_s3, _n, lambda *a, **k: None)
_fake_s3.list_s3_files = lambda *a, **k: []
sys.modules["aws.s3"] = _fake_s3

# Two uploaded documents, deliberately NOT in timestamp order in the table. This
# is the shape of the original bug: the code that used to read this took the
# first rows a scan handed back and called them the newest.
_FILE_ROWS = [
    {"filename": "old-handbook.pdf", "uploaded_at": _iso(400)},
    {"filename": "new-handbook.pdf", "uploaded_at": _iso(3)},
]

_fake_ddb = types.ModuleType("aws.dynamodb")
for _n in ("upsert_conversation", "list_conversations", "get_conversation",
           "delete_conversation_from_db", "list_reports", "update_report_status",
           "save_file_metadata", "delete_file_from_db", "find_report_by_msg",
           "update_report_reason", "save_report"):
    setattr(_fake_ddb, _n, lambda *a, **k: None)
_fake_ddb.files_table = types.SimpleNamespace(scan=lambda *a, **k: {"Items": _FILE_ROWS})
_fake_ddb.conversations_table = types.SimpleNamespace(scan=lambda *a, **k: {"Items": []})
_fake_ddb.reports_table = types.SimpleNamespace(scan=lambda *a, **k: {"Items": []})
sys.modules["aws.dynamodb"] = _fake_ddb

_fake_students = types.ModuleType("aws.students")
for _n in ("get_student_by_uid", "get_student_by_email"):
    setattr(_fake_students, _n, lambda *a, **k: None)
_fake_students.format_student_context = lambda *a, **k: ""
_fake_students.list_students = lambda *a, **k: []
sys.modules["aws.students"] = _fake_students


class _AnyModule(types.ModuleType):
    """Any attribute is a no-op callable."""

    def __getattr__(self, name):
        return lambda *a, **k: None


# No user pool to list, so the "new account" rows are simply absent — which is
# also the assertion that a missing Cognito costs its own rows and nothing else.
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

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("AWS_REGION", "us-east-1")

from sc_assistant import create_app  # noqa: E402

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


admin_c = client_as("admin@sc.edu.ph", is_admin=True)
student_c = client_as("student@sc.edu.ph")
anon_c = client_as()

rules = sorted(str(r) for r in app.url_map.iter_rules())
for want in ("/api/dashboard/activities", "/api/dashboard/attention"):
    check(f"{want} mounted", any(r.startswith(want) for r in rules))

# Something for the feed to report, recreated because section 11 emptied the
# stores on purpose.
gaps.record_gap("Is there a shuttle service?", conv_id="c9")
escalation.create_escalation("Can I still enrol?", route="registrar",
                             student_email="student@sc.edu.ph")


r = admin_c.get("/api/dashboard/activities?limit=10")
check("admin gets 200 from the feed", r.status_code == 200)
payload = r.get_json()
check("feed reports success", payload.get("success") is True)
check("the window is stated, so the empty case can name it",
      payload.get("window_days") == activity.DEFAULT_WINDOW_DAYS)

rows = payload.get("activities") or []
check("the feed is not empty", len(rows) > 0)
for field in ("text", "icon", "time", "at", "tone"):
    check(f"rows carry '{field}'", all(field in row for row in rows))

# THE REGRESSION TEST. Both documents are in the table; only the recent one is
# inside the window, and it must outrank the gap and escalation logged just now
# only if it is genuinely newer. What must never happen again is the 400-minute
# -old file appearing while the 3-minute-old one is missing.
_texts = " | ".join(row["text"] for row in rows)
check("the recently uploaded document is in the feed", "new-handbook.pdf" in _texts)
check("DynamoDB rows are time-sorted with the rest, not appended",
      [row["at"] for row in rows] == sorted((row["at"] for row in rows), reverse=True))

# ?limit= is clamped rather than trusted: it reaches a slice on data an admin
# does not control the size of.
check("limit is honoured",
      len((admin_c.get("/api/dashboard/activities?limit=2").get_json()
           )["activities"]) <= 2)
check("junk limit falls back instead of 500ing",
      admin_c.get("/api/dashboard/activities?limit=abc").status_code == 200)
check("an absurd limit is clamped",
      len((admin_c.get("/api/dashboard/activities?limit=99999").get_json()
           )["activities"]) <= 50)

a = admin_c.get("/api/dashboard/attention")
check("admin gets 200 from the triage panel", a.status_code == 200)
apayload = a.get_json()
check("triage reports success", apayload.get("success") is True)
check("triage returns its rows", len(apayload.get("attention") or []) >= 3)
check("...and the health block", "health" in apayload)
check("the queue counts the escalation just filed",
      any(row["section"] == "escalations" and row["count"] >= 1
          for row in apayload["attention"]))

# Both are read-only and admin-only. This is a report of who complained and what
# went unanswered; a student must not be able to read it.
for path in ("/api/dashboard/activities", "/api/dashboard/attention"):
    check(f"student is refused {path}", student_c.get(path).status_code == 403)
    check(f"anonymous is refused {path}", anon_c.get(path).status_code == 403)
    check(f"{path} rejects POST", admin_c.post(path).status_code == 405)

# The old endpoint is gone, and with it the fabricated "+0%" beside every count.
check("the invented-trend stats endpoint is no longer routed",
      not any(r.startswith("/api/dashboard/stats") for r in rules))


# --------------------------------------------------------------------------- #
section("13. One screen, one copy of each figure")
# --------------------------------------------------------------------------- #
#
# The second bug in this area. The dashboard had TWO landing screens: "Overview"
# printed satisfaction, total ratings and unanswered count from
# /api/dashboard/attention, and "Analytics" printed the same three figures from
# /admin/analytics/api/overview. Two panels, two requests, two windows of time,
# one set of facts — so an admin could read 78% on one screen and 71% on the
# other and had no way to tell which was true. (They were computed over
# different periods: attention() is all-time, analytics is the selected range.)
#
# Overview is gone and its two panels moved onto Analytics. These assertions are
# about the rendered template, because that is where the duplication lived — the
# API was never wrong, it was displayed twice.

page = admin_c.get("/dashboard")
check("the dashboard renders", page.status_code == 200)
html = page.get_data(as_text=True)

# Exactly one landing screen, and it is Analytics.
check("Analytics is the section marked active",
      'id="analyticsSection" class="dashboard-section active"' in html)
check("there is only one active section on load",
      html.count('class="dashboard-section active"') == 1)

# The deleted screen must not come back. These are ID checks, and deliberately
# not label checks any more: the surviving screen has since TAKEN the name
# "Overview" (it is the queue, the feed and the headline figures — which is what
# an overview is), so grepping the HTML for that word now matches nine perfectly
# correct back buttons. What must never return is a SECOND section quoting the
# same figures, and that is what these ids were.
check("the second landing section is gone", 'id="overviewSection"' not in html)
check("...and its menu item with it", 'id="overviewMenuItem"' not in html)
check("...and no button navigates back to it", 'id="backToOverview"' not in html)

# THE REGRESSION TEST: the duplicated figures existed as three specific elements.
# If any of them reappears, some screen is printing a second copy of a number the
# Analytics KPI row already owns.
for dead_id in ("healthSatisfaction", "healthVotes", "healthUnanswered"):
    check(f"the duplicate '{dead_id}' element is gone", dead_id not in html)

# And the surviving copy is the KPI row, present exactly once.
for kpi in ("kpiSatisfaction", "kpiAnsweredRate", "kpiPending", "kpiQuestions"):
    check(f"'{kpi}' is rendered exactly once", html.count(f'id="{kpi}"') == 1)

# The two panels that moved must actually be on the page — a move that dropped
# them would also pass every assertion above.
check("the triage queue moved onto Analytics", 'id="attentionList"' in html)
check("the activity feed moved with it", 'class="activity-list' in html)
check("...and both appear once",
      html.count('id="attentionList"') == 1
      and html.count('class="activity-list') == 1)


# --------------------------------------------------------------------------- #
#  The landing screen must load ITSELF
#
#  Making Analytics the landing screen introduced a bug that every assertion
#  above still passed: the section is rendered with .active, so the admin arrives
#  on it without ever clicking the menu item — and every loader was attached to
#  that click. Nothing fetched. The KPIs sat on "—", the three canvases were
#  never drawn (blank white boxes), and the header still read "Dashboard"
#  instead of "Analytics", which is the visible fingerprint of showAnalytics()
#  never having run.
#
#  "The markup is present" is therefore not enough to test. What matters is that
#  something CALLS the loader on load.
# --------------------------------------------------------------------------- #
print("\n--- The landing screen loads on arrival ---")

# This load is now the FALLBACK arm of the restore-where-you-were check rather
# than an unconditional call, so the assertion follows it there. It is still the
# path taken on every first visit, when localStorage holds nothing.
check("the landing screen kicks off its own load",
      "if (!(window.restoreLastSection && window.restoreLastSection())) {" in html
      and "showAnalytics();" in html)

# It must be reachable by click too — the back buttons and the menu item.
check("...and clicking the menu item still loads it",
      "$('#analyticsMenuItem').on('click', window.showAnalytics)" in html)

# The header text is how a human tells that showAnalytics() ran, so keep the
# line that sets it.
check("arriving on the landing screen retitles the header",
      "$('.header-left span').text('Overview')" in html)

# One name for one screen. The menu item, the <h1> and the header title used to
# read "Analytics" while nine back buttons pointed at "Overview", so the same
# screen had two names depending on where you looked.
check("the menu item and the heading agree on the name",
      "<span>Overview</span>" in html and "<h1>Overview</h1>" in html)
check("...and no visible label still says Analytics",
      ">Analytics<" not in html and "text('Analytics')" not in html)


# --------------------------------------------------------------------------- #
#  A missing CDN must not blank the screen
#
#  Chart.js is loaded from jsdelivr. `new Chart` was called directly in the
#  middle of renderAnalytics(), so if that script did not arrive the exception
#  fired BEFORE the tables below it were filled in: one unavailable external
#  file took out the whole screen, including the figures that were already in
#  hand. Chart construction now goes through drawChart(), which degrades to a
#  message inside the chart's own box.
# --------------------------------------------------------------------------- #
print("\n--- The charts fail alone ---")

check("chart construction is funnelled through one guarded helper",
      html.count("new Chart(") == 1)
check("...and that helper checks the library actually loaded",
      "typeof Chart === 'undefined'" in html)
check("...and catches a construction failure",
      "catch (e)" in html and "This chart could not be drawn." in html)
check("all three charts go through it",
      html.count("= drawChart(") == 3)


# --------------------------------------------------------------------------- #
#  ...without fetching everything twice
#
#  dashboard.js used to call loadDashboardData() unconditionally at startup.
#  Now that the template also loads the landing screen (which calls loadTriage),
#  an unguarded call there would fire the triage and activity requests twice on
#  every page load.
# --------------------------------------------------------------------------- #
print("\n--- The triage queue loads exactly once ---")

with open(os.path.join("sc_assistant", "static", "js", "dashboard.js"),
          encoding="utf-8") as fh:
    dash_js = fh.read()

check("the startup triage load is guarded against a double fetch",
      "if (!triageLoadedOnce) loadDashboardData();" in dash_js)
check("...and the guard is actually set when it loads",
      "triageLoadedOnce = true;" in dash_js)
check("the template still drives the normal path",
      "window.loadTriage = loadDashboardData;" in dash_js)


# --------------------------------------------------------------------------- #
#  A reload keeps you where you were
#
#  All ten screens are one HTML document switched by an .active class, so a
#  reload always threw the admin back to the landing screen. That is the wrong
#  behaviour on a page you work in: uploading a document, reloading to check it
#  indexed, and losing your place every time is a loop with no way out.
#
#  These are source assertions rather than behavioural ones — there is no DOM or
#  localStorage in this runner — so they are written against the three things
#  that would silently break the feature rather than against its happy path.
# --------------------------------------------------------------------------- #
print("\n--- A reload lands on the section you left ---")

check("the last section is remembered",
      "localStorage.setItem(LAST_SECTION_KEY" in dash_js)
check("...and restored on load",
      "window.restoreLastSection = function()" in dash_js)

# Delegated on the shared id suffix. Binding each of the ten items by hand would
# mean editing that list for every new screen, and forgetting is silent.
check("every menu item is recorded by one delegated handler",
      "'.menu-item[id$=\"MenuItem\"]'" in dash_js)

# The back buttons do not click a menu item, so the recorder above cannot see
# them. Without this, "Back to Overview" then reload would restore the section
# you had just left.
check("returning to the landing screen is recorded too",
      "window.rememberSection('analyticsMenuItem')" in html)

# Both localStorage calls must be guarded. Private browsing throws on setItem,
# and a dashboard must not fail to navigate because it could not take a note.
check("a storage failure cannot break navigation",
      dash_js.count("catch (e)") >= 2)

# THE REGRESSION TEST. A stored id that no longer exists — a section renamed or
# removed since the value was written — must not be trusted: triggering a click
# on nothing would leave every section hidden and the admin on a blank page.
check("a stale stored section is validated against the DOM",
      "if (!$item.length) return false;" in dash_js)
check("...and its shape is validated before use",
      'test(saved)' in dash_js)

# The subtle one. `restoreLastSection` must NOT report success by asking whether
# any .dashboard-section is active: #analyticsSection ships with that class in
# the markup, so the answer is yes even when the trigger did nothing, the
# showAnalytics() fallback gets skipped, and the landing screen sits there with
# its loaders never run — KPIs on "—", canvases blank. It asks about the clicked
# menu item instead, which only a bound handler can have activated.
check("success is judged by the menu item, not the pre-activated section",
      "return $item.hasClass('active');" in dash_js
      and "$('.dashboard-section.active').length" not in dash_js)


# --------------------------------------------------------------------------- #
section("14. The sidebar reads in a usable order")
# --------------------------------------------------------------------------- #
#
# Ten sections were added one at a time and the menu was left in build order, so
# finding a screen meant reading all ten labels and the most urgent item ("Ask a
# Human" — a student is waiting for a reply) sat eighth.
#
# It is now grouped by job, urgent group first. These assertions are about
# ORDER and GROUPING, which no other test in this file looks at: every menu item
# is bound by id, so a reshuffle cannot fail loudly, it can only quietly put the
# rarely-used screens back on top.

positions = {}
for item_id in ("analyticsMenuItem", "escalationsMenuItem", "reportsMenuItem",
                "gapsMenuItem", "feedbackMenuItem", "conflictsMenuItem",
                "uploadsMenuItem", "calendarMenuItem", "announcementsMenuItem",
                "usersMenuItem"):
    marker = f'id="{item_id}"'
    check(f"'{item_id}' is in the menu exactly once", html.count(marker) == 1)
    positions[item_id] = html.index(marker)


def in_order(*ids):
    """True when these ids appear in the document in the order given."""
    places = [positions[i] for i in ids]
    return places == sorted(places)


# Analytics is the landing screen, so nothing may sit above it.
check("Analytics leads the menu",
      positions["analyticsMenuItem"] == min(positions.values()))

# The triage group, in the order rag.activity.attention() ranks the same
# concerns. If these two ever disagree, the sidebar and the panel on Analytics
# tell an admin different things about what matters most.
check("a waiting student outranks everything else in the queue",
      in_order("escalationsMenuItem", "reportsMenuItem", "gapsMenuItem",
               "feedbackMenuItem", "conflictsMenuItem"))
check("...and the queue as a whole outranks the knowledge base",
      in_order("conflictsMenuItem", "uploadsMenuItem"))
check("Ask a Human is no longer buried mid-list",
      sorted(positions.values()).index(positions["escalationsMenuItem"]) == 1)

# Knowledge base, then the rarely-used account screen.
check("uploads leads the knowledge-base group",
      in_order("uploadsMenuItem", "calendarMenuItem", "announcementsMenuItem"))
check("User Management sits below the day-to-day work",
      positions["usersMenuItem"] == max(positions.values()))

# Group headings must exist, and must not be menu items: dashboard.js calls
# $('.menu-item').removeClass('active') on every navigation, so a heading
# carrying that class would be styled as a section and could look "selected".
for label in ("Needs attention", "Knowledge base", "Administration"):
    check(f"the '{label}' heading is present",
          f'<div class="menu-group"><span>{label}</span></div>' in html)
check("headings are not clickable menu items",
      'class="menu-group menu-item"' not in html
      and 'class="menu-item menu-group"' not in html)

# The chatbot link leaves the dashboard entirely (target=_blank), so it is below
# a divider rather than flush in the list where it read as an eleventh section.
check("a divider separates the chatbot link", 'class="menu-divider"' in html)
check("...and it is the last thing in the menu",
      html.index('class="menu-divider"') > max(positions.values()))
check("the chatbot link still opens in a new tab",
      'target="_blank"' in html and 'rel="noopener noreferrer"' in html)

# Every section now carries a title, so a collapsed sidebar (icons only) is
# still navigable by hover.
check("every menu item has a tooltip",
      html.count('MenuItem" title="') == len(positions))


print("\n--- Badges survive the collapse ---")

# The seven badges were nested INSIDE the label span. Collapsed,
# `.sidebar.collapsed .menu-item span` hides labels, and a child of a
# display:none element cannot be brought back by any rule — so every count
# disappeared in exactly the state where a one-glance signal matters most.
# They are siblings of the label now.
for badge in ("sidebarEscalationBadge", "sidebarReportBadge", "sidebarGapBadge",
              "sidebarFeedbackBadge", "sidebarConflictBadge",
              "sidebarCalendarBadge", "sidebarAnnouncementsBadge"):
    check(f"'{badge}' is present", html.count(f'id="{badge}"') == 1)
    # The label span closes before the badge opens: proof they are siblings.
    before = html[:html.index(f'id="{badge}"')]
    check(f"...and '{badge}' is not nested in the label",
          before.rstrip().endswith("<span") and before.rstrip()[:-5].rstrip().endswith("</span>"))

with open(os.path.join("sc_assistant", "static", "css", "main.css"),
          encoding="utf-8") as fh:
    main_css = fh.read()


def rule_body(css, selector):
    """
    The declarations of one rule, and nothing else.

    Written after a mutation test caught this test lying: the first version
    searched `css.split(selector)[1]`, i.e. the whole remainder of the file, and
    `.user-dropdown` lower down also declares `display: block`. Deleting the
    declaration under test therefore still passed. Scoping to the closing brace
    is the difference between asserting about a rule and asserting about a file.
    """
    start = css.index(selector) + len(selector)
    return css[start:css.index("}", start)]


badge_rule = rule_body(main_css, ".sidebar.collapsed .menu-item .sc-count")
check("the collapsed badge restates display, or it inherits display:none",
      "display: block;" in badge_rule)
check("...and is positioned against the icon",
      "position: absolute;" in badge_rule)
check("...and hides the digits rather than shrinking them",
      "font-size: 0;" in badge_rule)
check("the collapsed menu item is a containing block for it",
      ".sidebar.collapsed .menu-item { position: relative; }" in main_css)
check("group headings collapse to a rule instead of unreadable text",
      ".sidebar.collapsed .menu-group span { display: none; }" in main_css)

# The badges used seven copies of the same five declarations inline, differing
# only in colour. They are classes now, so the shared part has one definition.
with open(os.path.join("sc_assistant", "static", "css",
                       "admin-components.css"), encoding="utf-8") as fh:
    admin_css = fh.read()

for variant in ("--red", "--green", "--blue", "--purple", "--amber"):
    check(f"'sc-count{variant}' is defined", f".sc-count{variant}{{" in admin_css)
check("no badge hardcodes border-radius:999px inline any more",
      "border-radius:999px; padding:0 6px" not in html)

# Amber cannot take white text at this size — white on #f59e0b is ~2.1:1.
check("the amber badge keeps dark text",
      ".sc-count--amber{background:#f59e0b;color:#111827}" in admin_css)


print("\n--- The reports badge is fetched once ---")

# sidebar.html ran its own IIFE against /admin/reports/api/list?status=pending,
# and dashboard.html requests the same URL for the same element on ready. Every
# dashboard load asked twice and the second answer overwrote the first with the
# same number. dashboard.html keeps it, because that copy is also re-run when a
# report is resolved.
check("only one request for the pending-report count",
      html.count("/admin/reports/api/list?status=pending") == 1)
check("...and the surviving copy still fills the badge",
      "$('#sidebarReportBadge').text(" in html)


# --------------------------------------------------------------------------- #

print(f"\n{'='*54}\n  {_passed} passed, {_failed} failed\n{'='*54}")

raise SystemExit(1 if _failed else 0)


