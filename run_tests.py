"""
run_tests.py — run every data-quality test in one command.

    python run_tests.py

Each suite is a standalone script that points its storage at a temp file, so the
real `conflict_resolutions.json` / `content_gaps.json` are never touched and no
AWS credentials are needed. They run in separate processes for that reason: the
env vars that redirect storage must be set before their module imports.

    test_store.py          file vs DynamoDB storage backend       (unit)
    test_gaps.py           storage + refusal detection + grouping (unit)
    test_citations.py      source-list grouping for answers       (unit)
    test_calendar.py       date parsing + deadline arithmetic     (unit)
    test_announcements.py  live window + audience + prompt block  (unit)
    test_language.py       Taglish/Waray -> English search terms  (unit)
    test_dictation.py      spoken "b s i t" -> a searchable query (unit)
    test_roles.py          who is asking -> which side of a process (unit)
    test_freshness.py      document dates + which source is newer (unit)
    test_doc_priority.py   which document supersedes which, by date (unit)
    test_reranker_model.py the app and the downloader load one model (unit)

    test_latency.py        when the optimizer round-trip is skipped (unit)
    test_progress.py       what the waiting student is told, truthfully (unit)




    test_stream_fallback.py provider refusal -> fallback, not error (unit)


    test_gaps_api.py       Content Gaps admin endpoints          (integration)
    test_conflicts_api.py  Data Conflicts endpoints + the prompt (integration)
    test_feedback_api.py   Answer votes + Ask-a-Human queue      (integration)
    test_calendar_api.py   Academic Calendar endpoints + preview (integration)
    test_announcements_api.py  endpoints + the public banner     (integration)
    test_freshness_api.py  document dates + pre-index scan       (integration)
    test_pwa.py            manifest, worker scope, offline page  (integration)
    test_activity.py       dashboard feed ordering + triage queue (integration)
    tools/check_data_conflicts.py  the real PDFs, as a report    (smoke)


The suites live in `tests/`. They are launched by filename from here but RUN with
the repo root as their working directory, because several of them read source files
by relative path (`sc_assistant/templates/base.html` and friends) and would
otherwise look under `tests/`.


"""





import os
import subprocess
import sys

# Where the suites are stored...
TESTS_DIR = "tests"
# ...and where they must run from. Derived from this file's own location, not the
# caller's CWD, so `python path/to/run_tests.py` from anywhere behaves the same.
RUN_FROM = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    # Storage first: if the seam under all four stores is broken, every failure
    # below it is a symptom and reading them in order wastes time.
    ("Storage backend (file/DynamoDB)",  "test_store.py"),
    ("Content-gap storage & detection", "test_gaps.py"),
    ("Answer source citations",         "test_citations.py"),
    ("Calendar awareness (dates)",      "test_calendar.py"),
    ("Announcements (live window)",     "test_announcements.py"),
    ("Taglish/Waray query expansion",   "test_language.py"),
    # Immediately after the language suite: both repair a query before retrieval
    # sees it, and both are additive for input that needs no repair. A failure in
    # one is usually the same mistake as a failure in the other.
    ("Dictated query repair (voice)",   "test_dictation.py"),

    # Who is asking. Sits next to the announcements suite because it owns the
    # audience mapping those notices are filtered by.
    ("Role-aware answers (asker)",      "test_roles.py"),
    # Which of two contradicting documents is current. Runs before the API

    # suites because the date comparison is what those endpoints expose.
    ("Document freshness (dates)",       "test_freshness.py"),

    # Immediately after it, because it is the same question one layer up: that
    # suite checks a date can be READ from a document, this one checks the date
    # is what decides precedence — replacing a hardcoded filename in the prompt
    # builder. A failure here with freshness passing means the wiring broke, not
    # the parsing.
    ("Document precedence (newest wins)", "test_doc_priority.py"),


    # Before the latency suite, because it decides whether the cross-encoder
    # runs AT ALL. When the app and download_model.py name different models the
    # reranker silently never loads: every document comes back without a score
    # and ranking degrades to raw retrieval order, on every machine except the
    # one whose HuggingFace cache happens to hold the right weights.
    ("Reranker model wiring (one id)",   "test_reranker_model.py"),

    # Last of the query-path unit suites, and deliberately after them: it decides
    # WHEN the optimizer runs, while the three above decide WHAT is searched. If
    # the language/dictation suites are failing, a latency failure is downstream
    # noise.
    ("Answer latency (skip + budget)",   "test_latency.py"),



    # Paired with the latency suite: that one makes the wait shorter, this one
    # makes it legible. Both are about the same seconds, and both must not change
    # what retrieval finds — so a failure in either is read against the other.
    ("Answer progress (what's showing)", "test_progress.py"),


    # A provider refusal must fall over to the other gateway, not surface as

    # "Streaming interrupted." on a question the handbook answers in full.
    ("LLM streaming fallback",          "test_stream_fallback.py"),
    ("Content Gaps admin API",          "test_gaps_api.py"),



    ("Data Conflicts admin API",        "test_conflicts_api.py"),
    ("Answer feedback & escalations",   "test_feedback_api.py"),
    ("Academic Calendar admin API",     "test_calendar_api.py"),
    ("Announcements API + banner",      "test_announcements_api.py"),
    ("Document freshness admin API",     "test_freshness_api.py"),
    # The installable app. Last of the integration suites because a broken
    # manifest or worker scope degrades the delivery of everything above it
    # without changing a single answer.
    ("Installable app (PWA + voice)",   "test_pwa.py"),


    # Last two, because they read what all of the above write: if the stores are
    # wrong, the dashboard built on top of them is wrong for a reason already
    # reported higher up this list.
    ("Analytics (metrics + endpoints)", "test_analytics.py"),
    ("Dashboard activity + triage",     "test_activity.py"),
]







results = []
for label, script in SUITES:
    print(f"\n{'#'*70}\n#  {label}  ({script})\n{'#'*70}")
    code = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.join(TESTS_DIR, script)],
        cwd=RUN_FROM,
    ).returncode
    results.append((label, script, code))

print(f"\n{'='*70}\n  SUMMARY\n{'='*70}")
for label, script, code in results:
    print(f"  {'PASS' if code == 0 else 'FAIL'}  {label:38} {script}")

failed = [r for r in results if r[2] != 0]
print(f"\n  {len(results) - len(failed)}/{len(results)} suites passed\n")
raise SystemExit(1 if failed else 0)
