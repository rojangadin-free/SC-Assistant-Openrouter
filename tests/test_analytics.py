"""
test_analytics.py — the analytics module and its admin endpoints.

Run:  python tests/test_analytics.py

Everything analytics reports is DERIVED from three stores, so this suite seeds
those stores with known rows at known timestamps and then asserts the numbers.
The point is not "does it return a dict" — it is that the four claims most likely
to be quietly wrong are actually right:

  1. Timestamps are bucketed in **Manila** time, not UTC. A vote at 23:30 UTC
     happened at 07:30 the NEXT DAY on campus. If this is wrong, every "peak
     hour" and every daily total is off by up to 8 hours, and nobody would
     notice from looking at the chart.

  2. "No data" is distinguishable from "zero percent". `_rate()` returns None
     when there is nothing to divide, because a brand-new deployment and a
     completely broken corpus must not render identically.

  3. Analytics groups topics with the SAME normaliser as the gap log, so the top
     row here matches the top row there.

  4. The endpoints are admin-only and read-only.

Storage is pointed at a temp directory before any import, so the real
answer_feedback.json / content_gaps.json / escalations.json are untouched and no
AWS credentials are needed.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import datetime
import json
import os
import sys
import tempfile
import types

_tmp = tempfile.mkdtemp(prefix="sc_analytics_test_")
os.environ["STORE_BACKEND"] = "file"
os.environ["ANSWER_FEEDBACK_FILE"] = os.path.join(_tmp, "answer_feedback.json")
os.environ["ESCALATIONS_FILE"] = os.path.join(_tmp, "escalations.json")
os.environ["CONTENT_GAPS_FILE"] = os.path.join(_tmp, "content_gaps.json")
os.environ["CONFLICT_RESOLUTIONS_FILE"] = os.path.join(_tmp, "conflict_resolutions.json")
os.environ["CALENDAR_PERIODS_FILE"] = os.path.join(_tmp, "calendar_periods.json")
os.environ["ANNOUNCEMENTS_FILE"] = os.path.join(_tmp, "announcements.json")
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
    """Any attribute is a no-op."""

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
from rag import analytics                    # noqa: E402
from rag.gaps import normalize_question      # noqa: E402

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


# ---------------------------------------------------------------------------
# Seed data.
#
# Written straight to the JSON files rather than through record_vote() /
# record_gap(), because those stamp "now" and this suite needs to assert on
# specific hours and specific days. That is the whole point of tests 1 and 4.
# ---------------------------------------------------------------------------
TODAY = datetime.date(2026, 3, 10)          # a fixed "today" so deltas are stable


def utc(day: datetime.date, hour: int, minute: int = 0) -> str:
    return datetime.datetime(day.year, day.month, day.day, hour, minute,
                             tzinfo=datetime.timezone.utc).isoformat()


Q_LATIN = "What are the requirements for latin honors?"
Q_SHUTTLE = "Is there a shuttle service to the campus?"

_votes = {
    # 23:30 UTC on Mar 9 == 07:30 Manila on Mar 10. This single row is the
    # timezone test: bucketed in UTC it lands on the wrong DAY and the wrong HOUR.
    "m1": {
        "msg_id": "m1", "verdict": "up", "question": Q_LATIN,
        "topic_key": normalize_question(Q_LATIN),
        "answer_snippet": "Summa cum laude requires 1.00-1.20…",
        "sources": ["Samar-College-update.pdf p.52"], "comment": "",
        "voter": "s1@sc.edu.ph", "conv_id": "c1",
        "at": utc(datetime.date(2026, 3, 9), 23, 30),
    },
    "m2": {
        "msg_id": "m2", "verdict": "up", "question": Q_LATIN,
        "topic_key": normalize_question(Q_LATIN),
        "answer_snippet": "…", "sources": ["Samar-College-update.pdf p.52"],
        "comment": "", "voter": "s2@sc.edu.ph", "conv_id": "c2",
        "at": utc(TODAY, 2, 0),                      # 10:00 Manila
    },
    "m3": {
        "msg_id": "m3", "verdict": "down", "question": "Who is the dean of education?",
        "topic_key": normalize_question("Who is the dean of education?"),
        "answer_snippet": "Nimfa T. Torremoro…",
        "sources": ["Samar-College-update.pdf p.15"], "comment": "wrong name",
        "voter": "s3@sc.edu.ph", "conv_id": "c3",
        "at": utc(TODAY, 2, 30),                     # 10:30 Manila
    },
    # A vote from the PRIOR week, so week_over_week has something to compare.
    "m4": {
        "msg_id": "m4", "verdict": "down", "question": Q_LATIN,
        "topic_key": normalize_question(Q_LATIN),
        "answer_snippet": "…", "sources": ["Samar-College-update.pdf p.52"],
        "comment": "", "voter": "s4@sc.edu.ph", "conv_id": "c4",
        "at": utc(TODAY - datetime.timedelta(days=9), 3, 0),
    },
    # Deliberately corrupt: a row we cannot date must be skipped, not crash.
    "m5": {
        "msg_id": "m5", "verdict": "up", "question": "anything",
        "topic_key": "anything", "answer_snippet": "", "sources": [],
        "comment": "", "voter": "", "conv_id": "", "at": "not-a-timestamp",
    },
}

_gaps = {
    normalize_question(Q_SHUTTLE): {
        "key": normalize_question(Q_SHUTTLE),
        "topic": Q_SHUTTLE,
        "count": 3,
        "status": "open",
        "examples": [
            {"question": Q_SHUTTLE, "at": utc(TODAY, 1, 0)},        # 09:00 Manila
            {"question": "shuttle schedule?", "at": utc(TODAY, 5, 0)},
        ],
        "first_seen": utc(TODAY, 1, 0),
        "last_seen": utc(TODAY, 5, 0),
    },
}

_escalations = {
    "e1": {
        "id": "e1", "question": Q_SHUTTLE,
        "topic_key": normalize_question(Q_SHUTTLE),
        "route": "registrar", "route_label": "Registrar",
        "student_email": "s1@sc.edu.ph", "contact": "", "reply_to": "email",
        "note": "", "bot_answer": "", "conv_id": "c9",
        "status": "answered", "reply": "No shuttle service is provided.",
        "answered_by": "admin@sc.edu.ph",
        "answered_at": utc(TODAY, 6, 0),          # 4 hours after it was raised
        "read_at": "", "created_at": utc(TODAY, 2, 0),
    },
    "e2": {
        "id": "e2", "question": "Do you accept transferees mid-semester?",
        "topic_key": normalize_question("Do you accept transferees mid-semester?"),
        "route": "registrar", "route_label": "Registrar",
        "student_email": "s2@sc.edu.ph", "contact": "", "reply_to": "email",
        "note": "", "bot_answer": "", "conv_id": "c10",
        "status": "pending", "reply": "", "answered_by": "", "answered_at": "",
        "read_at": "", "created_at": utc(TODAY - datetime.timedelta(days=2), 2, 0),
    },
}


def seed():
    with open(os.environ["ANSWER_FEEDBACK_FILE"], "w", encoding="utf-8") as fh:
        json.dump({"votes": _votes, "topics": {}}, fh)
    with open(os.environ["CONTENT_GAPS_FILE"], "w", encoding="utf-8") as fh:
        json.dump({"gaps": _gaps}, fh)
    with open(os.environ["ESCALATIONS_FILE"], "w", encoding="utf-8") as fh:
        json.dump({"items": _escalations}, fh)


seed()


# ============================================================
section("1. Timestamps are bucketed in Manila time, not UTC")
# ============================================================
events = analytics._events()

m1 = [e for e in events if e["kind"] == "answered" and e["hour"] == 7]
check("23:30 UTC lands at 07:30 Manila", len(m1) == 1,
      f"hours seen: {sorted(e['hour'] for e in events)}")
check("…and on the NEXT calendar day",
      bool(m1) and m1[0]["date"] == TODAY,
      f"got {m1[0]['date'] if m1 else None}, want {TODAY}")

hist = analytics.hourly_histogram()
check("histogram has 24 buckets", len(hist) == 24, str(len(hist)))
check("07:00 bucket counted it", hist[7]["count"] == 1, str(hist[7]))
check("no activity recorded at 23:00", hist[23]["count"] == 0, str(hist[23]))
# 10:00 Manila holds four events, and they come from three different stores:
# two votes (02:00 and 02:30 UTC), one escalation raised (02:00 UTC) and one
# from the prior week (03:00 UTC -> 11:00, so not this bucket). The histogram is
# deliberately a view of ALL activity, not just votes — an admin asking "when is
# the assistant busy" means every kind of traffic.
check("10 AM is the peak", hist[10]["count"] == 4, str(hist[10]))
check("…and it is the tallest bar",
      hist[10]["share_of_peak"] == 1.0, str(hist[10]["share_of_peak"]))

check("bucket labels are 12-hour", hist[13]["label"] == "1 PM", hist[13]["label"])


# ============================================================
section("2. A row with an unparseable timestamp is skipped, not fatal")
# ============================================================
check("the corrupt vote produced no event",
      not any(e["label"] == "anything" for e in events))
check("the other four votes survived",
      sum(1 for e in events if e["kind"] == "answered") == 4,
      str(sum(1 for e in events if e["kind"] == "answered")))


# ============================================================
section("3. Headline totals")
# ============================================================
ov = analytics.overview(14, today=TODAY)
t = ov["totals"]

check("4 answered (one per vote)", t["answered"] == 4, str(t["answered"]))
check("2 unanswered (one per gap EXAMPLE)", t["unanswered"] == 2, str(t["unanswered"]))
check("2 up / 2 down", (t["up"], t["down"]) == (2, 2), f"{t['up']}/{t['down']}")
check("satisfaction = 2/4 = 0.5", t["satisfaction"] == 0.5, str(t["satisfaction"]))
check("answered rate = 4/6 ≈ 0.667", t["answered_rate"] == 0.667, str(t["answered_rate"]))
check("2 escalations raised", t["escalated"] == 2, str(t["escalated"]))
check("1 escalation answered", t["escalations_resolved"] == 1,
      str(t["escalations_resolved"]))
check("peak hour is reported", ov["peak_hour"] == "10 AM", str(ov["peak_hour"]))


# ============================================================
section("4. 'No data' is not the same as 'zero percent'")
# ============================================================
check("_rate(0, 0) is None, not 0.0", analytics._rate(0, 0) is None)
check("_rate(0, 5) IS 0.0", analytics._rate(0, 5) == 0.0)

# A day with no activity must appear in the series, with a None rate rather than
# a confident-looking 0%.
series = ov["series"]
check("series is zero-filled to the window", len(series) == 14, str(len(series)))
check("series ends on 'today'", series[-1]["date"] == TODAY.isoformat(),
      series[-1]["date"])
check("series is chronological", series[0]["date"] < series[-1]["date"])

quiet = [d for d in series if d["total"] == 0]
check("quiet days exist in this fixture", len(quiet) > 0)
check("a quiet day's rate is None, not 0",
      all(d["answered_rate"] is None for d in quiet))

today_row = series[-1]
check("today: 3 answered", today_row["answered"] == 3, str(today_row))
check("today: 2 unanswered", today_row["unanswered"] == 2, str(today_row))
check("today's rate = 3/5 = 0.6", today_row["answered_rate"] == 0.6,
      str(today_row["answered_rate"]))


# ============================================================
section("5. Topics group with the SAME normaliser as the gap log")
# ============================================================
topics = analytics.top_topics(10)
latin_key = normalize_question(Q_LATIN)

check("the latin-honors key matches rag.gaps",
      any(r["key"] == latin_key for r in topics),
      str([r["key"] for r in topics]))

latin = next(r for r in topics if r["key"] == latin_key)
check("3 votes collapsed into one topic", latin["total"] == 3, str(latin["total"]))
check("…with 2 up and 1 down", (latin["up"], latin["down"]) == (2, 1),
      f"{latin['up']}/{latin['down']}")
check("topic satisfaction = 2/3 ≈ 0.667", latin["satisfaction"] == 0.667,
      str(latin["satisfaction"]))
# The shuttle topic also has 3 events (2 gap examples + 1 escalation), so it ties
# with latin honors. A tie must break deterministically or the dashboard would
# reorder itself between refreshes with no data having changed.
check("the list is sorted by volume, descending",
      [r["total"] for r in topics] == sorted((r["total"] for r in topics), reverse=True),
      str([(r["key"], r["total"]) for r in topics]))
check("a tie breaks on the label, not on dict order",
      [r["key"] for r in topics if r["total"] == 3]
      == sorted(r["key"] for r in topics if r["total"] == 3),
      str([r["key"] for r in topics if r["total"] == 3]))


unanswered = analytics.top_topics(10, kind="unanswered")
check("the unanswered list is the document roadmap",
      [r["key"] for r in unanswered] == [normalize_question(Q_SHUTTLE)],
      str([r["key"] for r in unanswered]))
check("…and it is labelled readably",
      unanswered[0]["topic"] == Q_SHUTTLE, unanswered[0]["topic"])
check("a 'kind' filter excludes answered topics",
      all(r["answered"] == 0 for r in unanswered))


# ============================================================
section("6. Downvotes point at the page that produced them")
# ============================================================
docs = analytics.suspect_documents(10)
check("only pages with a downvote are listed", len(docs) == 2, str(docs))

worst = docs[0]
check("p.15 (the wrong dean) is listed",
      any(d["source"].endswith("p.15") for d in docs), str(docs))
check("p.52 has 2 up / 1 down",
      any(d["source"].endswith("p.52") and (d["up"], d["down"]) == (2, 1)
          for d in docs), str(docs))
p15 = next(d for d in docs if d["source"].endswith("p.15"))
check("a page with only downvotes scores 0.0 satisfaction",
      p15["satisfaction"] == 0.0, str(p15))
check("it outranks the page with the better ratio",
      worst["source"].endswith("p.15") or worst["down"] >= p15["down"],
      str(worst))


# ============================================================
section("7. Escalation clock")
# ============================================================
rt = analytics.response_times()
check("1 answered escalation", rt["answered_count"] == 1, str(rt))
check("it took 4 hours", rt["avg_hours"] == 4.0, str(rt["avg_hours"]))
check("1 still pending", rt["pending_count"] == 1, str(rt))
check("the pending one has a visible age",
      rt["oldest_pending_hours"] is not None and rt["oldest_pending_hours"] > 0,
      str(rt["oldest_pending_hours"]))


# ============================================================
section("8. Week-over-week compares, and admits when it cannot")
# ============================================================
trend = analytics.week_over_week(today=TODAY)
check("recent week saw 5 questions", trend["recent"]["questions"] == 5,
      str(trend["recent"]))
check("prior week saw 1", trend["prior"]["questions"] == 1, str(trend["prior"]))
check("the question delta is +4", trend["delta"]["questions"] == 4,
      str(trend["delta"]))
check("recent satisfaction = 2/3", trend["recent"]["satisfaction"] == 0.667,
      str(trend["recent"]["satisfaction"]))
check("prior satisfaction = 0/1", trend["prior"]["satisfaction"] == 0.0,
      str(trend["prior"]["satisfaction"]))
check("satisfaction delta is computed", trend["delta"]["satisfaction"] == 0.667,
      str(trend["delta"]["satisfaction"]))

# Move the window somewhere with no data at all: the delta must go None rather
# than claim a 0% change.
empty = analytics.week_over_week(today=datetime.date(2020, 1, 1))
check("an empty window yields no satisfaction figure",
      empty["recent"]["satisfaction"] is None, str(empty["recent"]))
check("…and therefore no delta, rather than a fake 0",
      empty["delta"]["satisfaction"] is None, str(empty["delta"]))


# ============================================================
section("9. Endpoints: mounted, admin-only, read-only")
# ============================================================
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
for want in (
    "/admin/analytics/api/overview",
    "/admin/analytics/api/topics",
    "/admin/analytics/api/documents",
):
    check(f"{want} mounted", any(r.startswith(want) for r in rules))

r = admin_c.get("/admin/analytics/api/overview?days=14")
check("admin gets 200", r.status_code == 200, str(r.status_code))
body = r.get_json()
check("payload is shaped for the dashboard",
      body["success"] and set(body["analytics"]) >= {
          "totals", "series", "hourly", "top_topics", "trend",
          "response_times", "suspect_documents", "peak_hour"},
      str(sorted(body.get("analytics", {}).keys())))
check("the endpoint agrees with the module",
      body["analytics"]["totals"]["answered"] == t["answered"],
      f"{body['analytics']['totals']['answered']} vs {t['answered']}")

check("student is refused", student_c.get("/admin/analytics/api/overview").status_code == 403)
check("anonymous is refused", anon_c.get("/admin/analytics/api/overview").status_code == 403)
check("student cannot read topics",
      student_c.get("/admin/analytics/api/topics").status_code == 403)
check("student cannot read documents",
      student_c.get("/admin/analytics/api/documents").status_code == 403)

# There must be no way to write a metric — the numbers are evidence, not content.
check("overview rejects POST",
      admin_c.post("/admin/analytics/api/overview").status_code == 405,
      str(admin_c.post("/admin/analytics/api/overview").status_code))

# A junk ?days= should degrade to the default, not 400 or explode.
r = admin_c.get("/admin/analytics/api/overview?days=abc")
check("junk ?days= falls back to the default",
      r.status_code == 200 and r.get_json()["analytics"]["window_days"] == analytics.DEFAULT_DAYS,
      str(r.get_json().get("analytics", {}).get("window_days")))

r = admin_c.get("/admin/analytics/api/overview?days=99999")
check("an absurd ?days= is clamped, not honoured",
      r.status_code == 200 and r.get_json()["analytics"]["window_days"] == 365,
      str(r.get_json().get("analytics", {}).get("window_days")))

r = admin_c.get("/admin/analytics/api/topics?kind=bogus")
check("an unknown ?kind= is rejected", r.status_code == 400, str(r.status_code))

r = admin_c.get("/admin/analytics/api/topics?kind=unanswered")
check("the roadmap is reachable over HTTP",
      r.status_code == 200 and len(r.get_json()["topics"]) == 1,
      str(r.get_json()))


# ============================================================
section("10. Empty stores read as 'nothing yet', not as failure")
# ============================================================
for var in ("ANSWER_FEEDBACK_FILE", "CONTENT_GAPS_FILE", "ESCALATIONS_FILE"):
    with open(os.environ[var], "w", encoding="utf-8") as fh:
        json.dump({}, fh)

blank = analytics.overview(7, today=TODAY)
check("no rows -> zero counts", blank["totals"]["answered"] == 0,
      str(blank["totals"]))
check("no rows -> None rate, not 0%", blank["totals"]["answered_rate"] is None,
      str(blank["totals"]["answered_rate"]))
check("no rows -> no peak hour claimed", blank["peak_hour"] is None,
      str(blank["peak_hour"]))
check("the series is still drawn", len(blank["series"]) == 7, str(len(blank["series"])))
check("suspect documents is empty, not missing", blank["suspect_documents"] == [])
check("the endpoint still returns 200 on an empty corpus",
      admin_c.get("/admin/analytics/api/overview").status_code == 200)


# ------------------------------------------------------------------ summary
print("\n" + "=" * 60)
print(f"  {_passed} passed, {_failed} failed")
print("=" * 60)
raise SystemExit(1 if _failed else 0)
