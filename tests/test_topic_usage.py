"""Offline topic taxonomy, private monthly counters, and atomic retry coverage."""

import concurrent.futures
import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
from test_storage_concurrency import ConditionalTable, Conflict

from rag import store, topic_usage

AT = datetime(2026, 11, 13, tzinfo=timezone.utc)
TODAY = date(2026, 11, 13)


def digest(event):
    return hashlib.sha256(b"sc-assistant:topic-usage:v1\0" + event.encode("utf-8")).hexdigest()


class TopicClassifierTests(unittest.TestCase):
    def test_curated_suggestions_match_their_public_topic(self):
        self.assertNotIn("other", topic_usage.TOPIC_QUESTIONS)
        for key, question in topic_usage.TOPIC_QUESTIONS.items():
            with self.subTest(topic=key):
                self.assertEqual(topic_usage.classify_topic(question), key)
                self.assertLessEqual(len(question), 200)

    def test_stable_public_taxonomy(self):
        self.assertEqual(topic_usage.TOPIC_LABELS, {
            "enrollment": "Admissions and enrollment",
            "fees": "Tuition and payments",
            "scholarships": "Scholarships",
            "programs": "Programs and courses",
            "calendar": "Schedules and deadlines",
            "offices": "Offices and contacts",
            "records": "Student records",
            "policies": "School policies",
            "facilities": "Campus services",
            "activities": "Student activities",
            "accounts": "Account access",
            "other": "Other questions",
        })

    def test_core_school_queries_in_english_and_filipino(self):
        cases = {
            "enrollment": [
                "How can I enroll at Samar College?", "What are the enrolment requirements?",
                "Ano ang requirements para sa pag-enroll?", "May admission exam ba?",
                "Paano mag enrol bilang transferee?", "When is the enrollment deadline?",
            ],
            "fees": [
                "What is the tuition fee for BSIT?", "Magkano ang matrikula?",
                "Saan pwedeng magbayad?", "May installment payments?",
                "When is the tuition payment deadline?",
            ],
            "scholarships": [
                "What scholarships are available?", "How do I apply for financial aid?",
                "May iskolar po ba?", "What are the scholarship renewal requirements?",
            ],
            "programs": [
                "Anong kursong meron sa Samar College?", "Do you offer BS Nursing?",
                "What is the BSIT curriculum?", "Which degree programs are available?",
            ],
            "calendar": [
                "Academic calendar for this semester?", "When do classes start?",
                "Kailan ang pasukan?", "Ano ang iskedyul ng klase?",
                "What is the exam schedule?",
            ],
            "offices": [
                "Who is the dean of the College of Education?", "Sino ang dekano?",
                "Registrar contact number?", "Saan ang tanggapan ng registrar?",
                "Where is the admissions office?",
            ],
            "records": [
                "How can I request my TOR from the registrar?", "Where can I check my grades?",
                "Nawala ang student ID ko.", "Paano kumuha ng report card?",
                "I need a certificate of enrollment.",
            ],
            "policies": [
                "What is the attendance policy?", "Are uniforms required?",
                "Ano ang patakaran sa pagliban?",
            ],
            "facilities": [
                "Library opening hours?", "May clinic o guidance counseling?",
                "May paradahan at canteen ba?", "Is campus Wi-Fi available?",
            ],
            "activities": [
                "Which student clubs can I join?", "Kailan ang intramurals?",
                "Paano sumali sa organisasyon?",
            ],
            "accounts": [
                "How do I reset my password?", "I cannot log in using my email.",
                "Hindi ako makalogin sa portal.", "Di makapasok sa account ko.",
            ],
            "other": [
                "Will it rain tomorrow?", "What are the requirements?",
                "Please write a poem.", "What are the requirements for a passport?",
            ],
        }
        for expected, questions in cases.items():
            for question in questions:
                with self.subTest(question=question):
                    self.assertEqual(topic_usage.classify_topic(question), expected)

    def test_empty_greetings_and_acknowledgements_never_reuse_history(self):
        history = [{"role": "user", "content": "How do I enroll?"}]
        for question in (None, "", " \n\t", "Hello!", "Hi, good morning po!", "Thank you!",
                         "Thanks a lot", "Okay, thanks po", "Maraming salamat po",
                         "Magandang hapon!", "Noted", "Got it", "Yes please", "No thanks",
                         "Thank you for your help", "Sige po", "Salamat sa tulong"):
            with self.subTest(question=question):
                self.assertIsNone(topic_usage.classify_topic(question, history=history))
        self.assertEqual(topic_usage.classify_topic("Hi, what is the tuition fee?"), "fees")

    def test_explicit_current_topic_beats_old_subjects(self):
        history = [{"role": "user", "content": "How do I enroll?"},
                   {"role": "assistant", "content": "Ask the registrar about enrollment."}]
        for question, expected in (
            ("What is the tuition?", "fees"), ("Who is the dean?", "offices"),
            ("How can I see my grades?", "records"), ("What clubs exist?", "activities"),
            ("What about scholarships?", "scholarships"),
            ("What about my dog?", "other"), ("What are the passport requirements?", "other"),
        ):
            with self.subTest(question=question):
                self.assertEqual(topic_usage.classify_topic(question, history=history), expected)

    def test_short_followups_use_only_recent_user_subjects(self):
        history = [{"role": "user", "content": "What scholarships are available?"},
                   {"role": "assistant", "content": "Tuition, grades, and enrollment."}]
        for question in ("Requirements?", "What are the requirements?", "And the steps?",
                         "How do I apply?", "Where can I submit them?", "How much?",
                         "How long does it take?", "What about it?", "Tell me more",
                         "Please explain more", "Ano po ang requirements?", "Paano naman?"):
            with self.subTest(question=question):
                self.assertEqual(topic_usage.classify_topic(question, history=history), "scholarships")
        followups = history + [{"role": "user", "content": "What are the requirements?"},
                               {"role": "assistant", "content": "These requirements."}]
        self.assertEqual(topic_usage.classify_topic("Where can I submit them?", history=followups),
                         "scholarships")
        self.assertEqual(topic_usage.classify_topic("Requirements?", history=tuple(history)), "scholarships")

    def test_unrelated_or_image_user_turn_stops_subject_carryover(self):
        old = [{"role": "user", "content": "How do I enroll?"}]
        for recent in ("What is the weather?", "What are the passport requirements?",
                       "What about my dog?", "[Image Uploaded]", "What is this? [Image Uploaded]", ""):
            history = old + [{"role": "user", "content": recent},
                             {"role": "assistant", "content": "Enrollment."}]
            with self.subTest(recent=recent):
                self.assertEqual(topic_usage.classify_topic("Requirements?", history=history), "other")
        stale = old + [{"role": "assistant", "content": "Enrollment."}] * 6
        self.assertEqual(topic_usage.classify_topic("Requirements?", history=stale), "other")
        assistant_only = [{"role": "assistant", "content": "Tuition fees"},
                          {"role": "system", "content": "Scholarships"}]
        self.assertEqual(topic_usage.classify_topic("Requirements?", history=assistant_only), "other")
        for history in (None, "enrollment", {}, [None, 1, {"role": "user", "content": None}]):
            self.assertEqual(topic_usage.classify_topic("Requirements?", history=history), "other")

    def test_pii_remains_transient_and_cannot_become_a_category(self):
        history = [{"role": "user", "content": "My student ID is 2026-12345."},
                   {"role": "assistant", "content": "Private answer."}]
        before = copy.deepcopy(history)
        question = "I am Example Person, email student@example.invalid. How do I reset my password?"
        with patch.object(topic_usage, "_store", side_effect=AssertionError("Classifier must not persist")):
            self.assertEqual(topic_usage.classify_topic(question, history=history), "accounts")
            self.assertEqual(topic_usage.classify_topic("Reach me at tuition.fees@example.invalid."), "other")
            self.assertEqual(topic_usage.classify_topic("See https://example.invalid/enrollment/fees"), "other")
            self.assertEqual(topic_usage.classify_topic("My name is Example Person, 192.0.2.7."), "other")
        self.assertEqual(history, before)

    def test_images_never_infer_content_or_reuse_ambiguous_history(self):
        history = [{"role": "user", "content": "What scholarships are offered?"}]
        for question in (None, "", "  ", "What is this?", "How much?", "Requirements?",
                         "Explain the attached image"):
            with self.subTest(question=question):
                self.assertEqual(topic_usage.classify_topic(question, history=history, has_image=True), "other")
        self.assertEqual(topic_usage.classify_topic("Explain this tuition receipt", history=history,
                                                   has_image=True), "fees")
        self.assertIsNone(topic_usage.classify_topic("Thank you", history=history, has_image=True))


class TopicStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="sc_topic_usage_")
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.path = self.directory / "2026-11.json"
        env = patch.dict(os.environ, {"STORE_BACKEND": "file", "TOPIC_USAGE_DIR": str(self.directory)})
        env.start()
        self.addCleanup(env.stop)
        no_cloud = patch.object(store, "_table", side_effect=AssertionError("No live tables"))
        no_cloud.start()
        self.addCleanup(no_cloud.stop)

    def test_suggestions_change_with_rankings_in_the_default_window(self):
        read = topic_usage.most_asked_topics
        with patch.object(topic_usage, "most_asked_topics", side_effect=lambda limit: read(limit, today=TODAY)) as ranked:
            self.assertEqual(topic_usage.suggested_questions(), list(topic_usage.DEFAULT_SUGGESTIONS))
            topic_usage.record_topic_usage("fees", "guest-fee-question", at=AT)
            first = topic_usage.suggested_questions()
            self.assertEqual(first[0], topic_usage.TOPIC_QUESTIONS["fees"])
            for i in range(2):
                topic_usage.record_topic_usage("scholarships", f"scholarship-{i}", at=AT)
            for i in range(4):
                topic_usage.record_topic_usage("other", f"unclassified-{i}", at=AT)
                topic_usage.record_topic_usage("policies", f"old-policy-{i}", at=AT - timedelta(days=20))
            self.assertEqual(topic_usage.suggested_questions(), [
                topic_usage.TOPIC_QUESTIONS["scholarships"], topic_usage.TOPIC_QUESTIONS["fees"],
                topic_usage.TOPIC_QUESTIONS["enrollment"],
            ])
            ranked.assert_called_with(len(topic_usage.TOPIC_LABELS))

    def test_suggestion_projection_never_exposes_raw_analytics(self):
        private = "Private student question <img src=x onerror=alert(1)>"
        with patch.object(topic_usage, "most_asked_topics", return_value=[
            {"key": "records", "topic": private, "total": 123, "recent_events": ["private-event"]},
            {"key": "records", "topic": private, "total": 100},
            {"key": "other", "topic": private, "total": 999},
            {"key": private, "total": 999}, {"key": []}, None,
        ]):
            questions = topic_usage.suggested_questions()
        self.assertEqual(questions, [topic_usage.TOPIC_QUESTIONS["records"], *topic_usage.DEFAULT_SUGGESTIONS[:2]])
        self.assertEqual(len(set(questions)), 3)
        self.assertNotIn(private, json.dumps(questions))
        self.assertNotIn("private-event", json.dumps(questions))

    def test_suggestion_read_failure_uses_defaults_without_logging_private_data(self):
        with patch.object(topic_usage, "most_asked_topics", side_effect=RuntimeError("private diagnostic")), \
                patch("builtins.print") as output:
            self.assertEqual(topic_usage.suggested_questions(), list(topic_usage.DEFAULT_SUGGESTIONS))
        output.assert_not_called()

    def test_store_name_and_configured_or_default_file_path(self):
        document = topic_usage._store("2026-11")
        self.assertEqual(document.name, "topic_usage:2026-11")
        self.assertEqual(document.path, str(self.path))
        with patch.dict(os.environ):
            os.environ.pop("TOPIC_USAGE_DIR", None)
            self.assertEqual(topic_usage._store("2026-11").path, os.path.join("topic_usage", "2026-11.json"))

    def test_missing_file_snapshots_are_empty_and_never_save(self):
        with patch.object(store.JsonBlobStore, "_save_file") as save, \
                patch.object(store.JsonBlobStore, "load", side_effect=AssertionError("Masks errors")):
            self.assertEqual(topic_usage.most_asked_topics(today=TODAY), [])
        save.assert_not_called()
        self.assertEqual(list(self.directory.glob("*.json")), [])

    def test_retry_once_separate_replies_add_and_snapshots_do_not_write(self):
        self.assertTrue(topic_usage.record_topic_usage("fees", "answer-one", at=AT))
        self.assertFalse(topic_usage.record_topic_usage("fees", "answer-one", at=AT))
        self.assertFalse(topic_usage.record_topic_usage("records", "answer-one", at=AT + timedelta(days=1)))
        self.assertTrue(topic_usage.record_topic_usage("fees", "answer-two", at=AT))
        before = self.path.read_bytes()
        with patch.object(store.JsonBlobStore, "_save_file") as save, \
                patch.object(store.JsonBlobStore, "load", side_effect=AssertionError("Masks errors")):
            rows = topic_usage.most_asked_topics(today=TODAY)
        self.assertEqual(rows, [{"key": "fees", "topic": "Tuition and payments", "total": 2}])
        save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(json.loads(before)["recent_events"], [digest("answer-one"), digest("answer-two")])

    def test_only_counts_hashes_and_store_timestamp_are_persisted(self):
        question = "My name is Private Person and my grades for student ID 2026-12345 are wrong."
        event_id = "private-msg-123|private@example.invalid|192.0.2.7|guest-session-secret"
        history = [{"role": "user", "content": "private-history-text"}]
        category = topic_usage.classify_topic(question, history=history, has_image=True)
        self.assertTrue(topic_usage.record_topic_usage(category, event_id, at=AT))
        serialized = self.path.read_text(encoding="utf-8")
        data = json.loads(serialized)
        self.assertEqual(set(data), {"days", "recent_events", "updated_at"})
        self.assertEqual(data["days"], {"2026-11-13": {"records": 1}})
        self.assertEqual(data["recent_events"], [digest(event_id)])
        self.assertNotEqual(data["recent_events"][0], hashlib.sha256(event_id.encode()).hexdigest())
        self.assertEqual(datetime.fromisoformat(data["updated_at"]).utcoffset(), timedelta(0))
        for private in (question, event_id, "Private Person", "2026-12345", "private@example.invalid",
                        "192.0.2.7", "guest-session-secret", "private-history-text", "private-msg-123"):
            self.assertNotIn(private, serialized)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_skip_and_invalid_arguments_do_not_open_storage(self):
        with patch.object(topic_usage, "_store") as factory:
            self.assertFalse(topic_usage.record_topic_usage(None, None))
            for topic in ("", "not-a-topic", "Fees", " tuition ", {"question": "private"}, 1):
                with self.subTest(topic=topic), self.assertRaises(ValueError):
                    topic_usage.record_topic_usage(topic, "test-event", at=AT)
            for event in (None, "", "  \n\t", 1, {}, b"event"):
                with self.subTest(event=event), self.assertRaises(ValueError):
                    topic_usage.record_topic_usage("fees", event, at=AT)
            for at in (datetime(2026, 11, 13), TODAY, "2026-11-13", 123):
                with self.subTest(at=at), self.assertRaises(ValueError):
                    topic_usage.record_topic_usage("fees", "test-event", at=at)
        factory.assert_not_called()

    def test_public_label_mutation_cannot_expand_the_storage_allowlist(self):
        with (
            patch.dict(topic_usage.TOPIC_LABELS, {"private raw question": "Private label"}),
            self.assertRaises(ValueError),
        ):
            topic_usage.record_topic_usage("private raw question", "test-event", at=AT)
        self.assertFalse(self.path.exists())

    def test_manila_midnight_month_and_year_boundaries(self):
        before = datetime(2026, 1, 31, 15, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=1)
        new_year = datetime(2025, 12, 31, 16, tzinfo=timezone.utc)
        topic_usage.record_topic_usage("fees", "before-midnight", at=before)
        topic_usage.record_topic_usage("records", "after-midnight", at=after)
        topic_usage.record_topic_usage("offices", "new-year", at=new_year)
        january = json.loads((self.directory / "2026-01.json").read_text(encoding="utf-8"))
        february = json.loads((self.directory / "2026-02.json").read_text(encoding="utf-8"))
        self.assertEqual(january["days"], {"2026-01-01": {"offices": 1}, "2026-01-31": {"fees": 1}})
        self.assertEqual(february["days"], {"2026-02-01": {"records": 1}})
        self.assertFalse((self.directory / "2025-12.json").exists())
        self.assertEqual(topic_usage.most_asked_topics(days=1, today=after),
                         [{"key": "records", "topic": "Student records", "total": 1}])
        self.assertEqual({row["key"] for row in topic_usage.most_asked_topics(days=2, today=after)},
                         {"fees", "records"})

    def test_selected_window_excludes_old_and_future_days_without_pruning(self):
        end = date(2026, 3, 1)
        for day, key in ((date(2026, 1, 15), "offices"), (date(2026, 2, 15), "offices"),
                         (date(2026, 2, 16), "fees"), (end, "fees"), (date(2026, 3, 2), "offices")):
            topic_usage.record_topic_usage(key, day.isoformat(), at=datetime.combine(day, datetime.min.time(),
                                                                                    tzinfo=timezone.utc))
        original = {path.name: path.read_bytes() for path in self.directory.glob("*.json")}
        self.assertEqual(topic_usage.most_asked_topics(today=end),
                         [{"key": "fees", "topic": "Tuition and payments", "total": 2}])
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.glob("*.json")}, original)
        (self.directory / "1900-01.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(topic_usage.most_asked_topics(today=end)[0]["total"], 2)

    def test_stable_ordering_and_limit_clamps(self):
        for key in reversed(topic_usage.TOPIC_LABELS):
            count = 3 if key in {"fees", "enrollment"} else 1
            for i in range(count):
                topic_usage.record_topic_usage(key, f"{key}-{i}", at=AT)
        rows = topic_usage.most_asked_topics(limit=1000, today=TODAY)
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows, sorted(rows, key=lambda row: (-row["total"], row["topic"])))
        self.assertEqual([row["key"] for row in rows[:2]], ["enrollment", "fees"])
        for limit in (0, -10, 1):
            self.assertEqual(topic_usage.most_asked_topics(limit, today=TODAY), rows[:1])
        self.assertEqual(topic_usage.most_asked_topics(2, today=TODAY), rows[:2])
        self.assertEqual(topic_usage.most_asked_topics(today=TODAY), rows[:10])

    def test_days_clamp_and_zero_counters(self):
        for offset, key in ((0, "fees"), (1, "records"), (364, "offices"), (365, "activities")):
            topic_usage.record_topic_usage(key, f"day-{offset}", at=AT - timedelta(days=offset))
        for days in (0, -100, 1):
            self.assertEqual(topic_usage.most_asked_topics(days=days, today=TODAY),
                             [{"key": "fees", "topic": "Tuition and payments", "total": 1}])
        self.assertEqual({row["key"] for row in topic_usage.most_asked_topics(days=1000, today=TODAY)},
                         {"fees", "records", "offices"})
        topic_usage._store("2026-11").mutate(lambda data: data["days"]["2026-11-13"].update(other=0))
        self.assertNotIn("other", {row["key"] for row in topic_usage.most_asked_topics(today=TODAY)})

    def test_invalid_query_dates_raise_without_storage(self):
        with patch.object(topic_usage, "_store") as factory:
            for today in (datetime(2026, 11, 13), "2026-11-13", 1):
                with self.subTest(today=today), self.assertRaises(ValueError):
                    topic_usage.most_asked_topics(today=today)
        factory.assert_not_called()

    def test_corrupt_json_and_schema_fail_closed_without_writing_or_logging(self):
        invalid = [
            "{broken", "[]", "{}",
            {"days": [], "recent_events": []},
            {"days": {"2026-11-31": {}}, "recent_events": []},
            {"days": {"2026-12-01": {}}, "recent_events": []},
            {"days": {"20261113": {}}, "recent_events": []},
            {"days": {"2026-11-13": {"private question": 1}}, "recent_events": []},
            {"days": {"2026-11-13": []}, "recent_events": []},
            {"days": {}, "recent_events": {}},
            {"days": {}, "recent_events": ["raw event id"]},
            {"days": {}, "recent_events": [None]},
            {"days": {}, "recent_events": [digest("one"), digest("one")]},
            {"days": {}, "recent_events": [digest(str(i)) for i in range(513)]},
            {"days": {}, "recent_events": [], "question": "private raw text"},
            {"days": {}, "recent_events": [], "updated_at": "not a timestamp"},
            {"days": {}, "recent_events": [], "updated_at": "2026-11-13T00:00:00"},
            {"days": {}, "recent_events": [], "updated_at": "x" * 41},
        ]
        invalid += [{"days": {"2026-11-13": {"fees": count}}, "recent_events": []}
                    for count in (True, -1, 1.5, "1", None, topic_usage._MAX_COUNT + 1)]
        for payload in invalid:
            serialized = payload if isinstance(payload, str) else json.dumps(payload)
            self.path.write_text(serialized, encoding="utf-8")
            with self.subTest(payload=serialized), patch.object(store.JsonBlobStore, "_save_file") as save, \
                    patch("builtins.print") as output:
                with self.assertRaises((ValueError, TypeError)):
                    topic_usage.most_asked_topics(today=TODAY)
                with self.assertRaises((ValueError, TypeError)):
                    topic_usage.record_topic_usage("fees", "answer-new", at=AT)
            save.assert_not_called()
            output.assert_not_called()
            self.assertEqual(self.path.read_text(encoding="utf-8"), serialized)

    def test_unavailable_file_reads_are_not_empty_data(self):
        with patch.object(store.JsonBlobStore, "_load_file", side_effect=PermissionError("unavailable")), \
                patch.object(store.JsonBlobStore, "_save_file") as save:
            with self.assertRaises(PermissionError):
                topic_usage.most_asked_topics(today=TODAY)
            with self.assertRaises(PermissionError):
                topic_usage.record_topic_usage("fees", "one", at=AT)
        save.assert_not_called()

    def test_retry_ring_is_bounded_and_eviction_does_not_remove_counts(self):
        self.assertEqual(topic_usage.MAX_RECENT_EVENTS, 512)
        events = [f"event-{i}" for i in range(512)]
        topic_usage._store("2026-11").save({
            "days": {"2026-11-13": {"fees": 512}}, "recent_events": [digest(event) for event in events],
        })
        self.assertTrue(topic_usage.record_topic_usage("fees", "event-512", at=AT))
        self.assertFalse(topic_usage.record_topic_usage("fees", "event-511", at=AT))
        self.assertTrue(topic_usage.record_topic_usage("fees", "event-0", at=AT))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["recent_events"]), 512)
        self.assertEqual(data["recent_events"], [digest(event) for event in events[2:] + ["event-512", "event-0"]])
        self.assertEqual(data["days"], {"2026-11-13": {"fees": 514}})

    def test_ring_is_month_local_not_a_permanent_replay_ledger(self):
        self.assertTrue(topic_usage.record_topic_usage("fees", "one-event", at=AT))
        self.assertTrue(topic_usage.record_topic_usage("fees", "one-event", at=AT + timedelta(days=31)))
        self.assertEqual(topic_usage.most_asked_topics(days=60, today=date(2026, 12, 14))[0]["total"], 2)

    def test_counter_overflow_is_rejected_without_losing_data(self):
        topic_usage._store("2026-11").save({
            "days": {"2026-11-13": {"fees": topic_usage._MAX_COUNT}}, "recent_events": [],
        })
        before = self.path.read_bytes()
        with self.assertRaises(OverflowError):
            topic_usage.record_topic_usage("fees", "new-event", at=AT)
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_real_file_increments_and_duplicate_suppression(self):
        events = ["same-event"] * 12 + [f"separate-{i}" for i in range(12)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda event: topic_usage.record_topic_usage("fees", event, at=AT), events))
        self.assertEqual(sum(results), 13)
        self.assertEqual(topic_usage.most_asked_topics(today=TODAY)[0]["total"], 13)
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["recent_events"]), 13)

    def test_missing_dynamodb_snapshots_and_known_months_never_put_or_scan(self):
        class TrackingTable(ConditionalTable):
            def __init__(self):
                super().__init__()
                self.reads = []

            def get_item(self, Key, ConsistentRead=False):
                self.reads.append(Key["store"])
                return super().get_item(Key=Key, ConsistentRead=ConsistentRead)

        table = TrackingTable()
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), \
                patch.object(store, "_table", return_value=table), \
                patch.object(store.JsonBlobStore, "load", side_effect=AssertionError("Masks errors")):
            self.assertEqual(topic_usage.most_asked_topics(days=9999, today=date(2026, 11, 13)), [])
        self.assertEqual(table.reads, ["topic_usage:2025-11", "topic_usage:2025-12"] +
                         [f"topic_usage:2026-{month:02d}" for month in range(1, 12)])
        self.assertEqual(len(table.reads), 13)
        self.assertEqual(table.writes, 0)
        self.assertEqual(table.items, {})
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_concurrent_same_and_distinct_events_survive_conditional_conflicts(self):
        table = ConditionalTable()
        barrier = threading.Barrier(24)

        def same(_):
            barrier.wait(timeout=10)
            return topic_usage.record_topic_usage("fees", "same-event", at=AT)

        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
                repeated = list(pool.map(same, range(24)))
            self.assertEqual(sum(repeated), 1)
            self.assertEqual(table.writes, 1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
                distinct = list(pool.map(lambda i: topic_usage.record_topic_usage(
                    "fees" if i % 2 else "records", f"event-{i}", at=AT,
                ), range(24)))
            self.assertTrue(all(distinct))
            writes = table.writes
            rows = topic_usage.most_asked_topics(today=TODAY)
            self.assertEqual(table.writes, writes)
        self.assertEqual(rows, [{"key": "fees", "topic": "Tuition and payments", "total": 13},
                                {"key": "records", "topic": "Student records", "total": 12}])
        self.assertEqual(table.writes, 25)
        self.assertGreater(table.conflicts, 0)

    def test_conditional_retries_capture_server_clock_and_month_once(self):
        attempted = []
        clock_calls = []

        class RetryOnce(ConditionalTable):
            def put_item(self, Item, **kwargs):
                payload = json.loads(Item["payload"])
                attempted.append((Item["store"], payload["days"], payload["recent_events"]))
                if len(attempted) == 1:
                    raise Conflict()
                return super().put_item(Item=Item, **kwargs)

        class AdvancingClock(datetime):
            @classmethod
            def now(cls, tz=None):
                clock_calls.append(tz)
                at = datetime(2026, 1, 31, 15, 59, 59, tzinfo=timezone.utc)
                return at + timedelta(seconds=len(clock_calls) - 1)

        table = RetryOnce()
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), \
                patch.object(store, "_table", return_value=table), \
                patch.object(topic_usage, "datetime", AdvancingClock):
            self.assertTrue(topic_usage.record_topic_usage("fees", "retry-event"))
        self.assertEqual(clock_calls, [timezone.utc])
        self.assertEqual(len(attempted), 2)
        self.assertEqual(attempted[0], attempted[1])
        self.assertEqual(attempted[0], ("topic_usage:2026-01", {"2026-01-31": {"fees": 1}},
                                       [digest("retry-event")]))
        self.assertEqual(set(table.items), {"topic_usage:2026-01"})

    def test_default_query_captures_one_server_manila_day(self):
        clock_calls = []

        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                clock_calls.append(tz)
                return datetime(2026, 1, 31, 16, tzinfo=timezone.utc)

        topic_usage.record_topic_usage("fees", "at-midnight", at=FixedClock.now(timezone.utc))
        clock_calls.clear()
        with patch.object(topic_usage, "datetime", FixedClock):
            rows = topic_usage.most_asked_topics(days=1)
        self.assertEqual(clock_calls, [timezone.utc])
        self.assertEqual(rows, [{"key": "fees", "topic": "Tuition and payments", "total": 1}])

    def test_failed_and_corrupt_dynamodb_reads_raise_without_puts(self):
        table = ConditionalTable()
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            topic_usage.record_topic_usage("fees", "existing", at=AT)
            before, writes = copy.deepcopy(table.items), table.writes
            table.fail_read = True
            with self.assertRaises(RuntimeError):
                topic_usage.most_asked_topics(today=TODAY)
            with self.assertRaises(RuntimeError):
                topic_usage.record_topic_usage("fees", "new", at=AT)
            self.assertEqual(table.items, before)
            self.assertEqual(table.writes, writes)
            table.fail_read = False
            for payload in ("{broken", "[]", "{}", json.dumps({"days": {}, "recent_events": ["raw"]})):
                table.items["topic_usage:2026-11"]["payload"] = payload
                with self.subTest(payload=payload):
                    with self.assertRaises((ValueError, TypeError)):
                        topic_usage.most_asked_topics(today=TODAY)
                    with self.assertRaises((ValueError, TypeError)):
                        topic_usage.record_topic_usage("fees", "new", at=AT)
                self.assertEqual(table.items["topic_usage:2026-11"]["payload"], payload)
                self.assertEqual(table.writes, writes)

    def test_worst_case_month_stays_small_under_the_real_dynamodb_guard(self):
        data = {
            "days": {f"2026-01-{day:02d}": {key: topic_usage._MAX_COUNT for key in topic_usage.TOPIC_LABELS}
                     for day in range(1, 32)},
            "recent_events": [digest(f"event-{i}") for i in range(topic_usage.MAX_RECENT_EVENTS)],
        }
        data["days"]["2026-01-01"]["other"] -= 1
        table = ConditionalTable()
        with patch.dict(os.environ, {"STORE_BACKEND": "dynamodb"}), patch.object(store, "_table", return_value=table):
            topic_usage._store("2026-01").save(data)
            self.assertTrue(topic_usage.record_topic_usage("other", "last-event",
                                                         at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        payload = table.items["topic_usage:2026-01"]["payload"]
        stored = json.loads(payload)
        self.assertEqual(len(stored["days"]), 31)
        self.assertTrue(all(set(counts) == set(topic_usage.TOPIC_LABELS) for counts in stored["days"].values()))
        self.assertTrue(all(count == topic_usage._MAX_COUNT for counts in stored["days"].values()
                            for count in counts.values()))
        self.assertEqual(len(stored["recent_events"]), 512)
        self.assertLess(len(payload.encode("utf-8")), 390 * 1024)
        self.assertLess(len(payload.encode("utf-8")), 64 * 1024)
        self.assertLess(len(json.dumps(stored, indent=2, ensure_ascii=False).encode("utf-8")), 64 * 1024)


if __name__ == "__main__":
    unittest.main()
