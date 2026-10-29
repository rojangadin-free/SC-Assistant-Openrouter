"""Standalone, offline load-tool regressions: python tests/test_loadtest_chat.py.

Execute the tool's actual stdlib definitions via AST, not a copied parser and not
the Locust registration block. No Locust dependency or application import. Flask
fixture checks skip clearly if Flask is absent; the full HTTP/Locust run is a
separate, explicitly selected transport/tool validation, not a capacity claim.
"""

import argparse
import ast
import importlib.util
import json
import os
import secrets
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "loadtest_chat.py"
source = ast.parse(TOOL.read_text(encoding="utf-8"), feature_version=(3, 10))
source.body = [node for node in source.body if not isinstance(node, ast.If)]
loadtest = types.ModuleType("_offline_loadtest_chat")
loadtest.__file__ = str(TOOL)
sys.modules[loadtest.__name__] = loadtest
exec(compile(source, str(TOOL), "exec"), loadtest.__dict__)  # noqa: S102 - execute owned stdlib definitions only


def frame(payload, newline="\n"):
    return ("data: " + json.dumps(payload, ensure_ascii=False) + newline + newline).encode("utf-8")


def parse(chunks, clock=lambda: 1.0):
    metrics = loadtest.StreamMetrics()
    loadtest.read_sse(chunks, metrics, 0.0, clock)
    return metrics


class StreamTests(unittest.TestCase):
    def test_every_byte_and_split_boundary(self):
        body = (b"\xef\xbb\xbf: heartbeat\r\n\r\n"
                + frame({"type": "phase", "phase": "writing"}, "\r\n")
                + frame({"type": "chunk", "text": "caf" + chr(233)}, "\r\n")
                + frame({"type": "done"}, "\r\n"))
        for split in range(len(body) + 1):
            with self.subTest(split=split):
                metrics = parse([body[:split], body[split:]])
                self.assertEqual(metrics.complete_ms, 1000)
                self.assertEqual(metrics.response_length, len(body))
        self.assertEqual(parse([bytes([b]) for b in body]).first_token_ms, 1000)

    def test_multiline_named_events_and_cr_only(self):
        body = (b'event: chunk\rdata: {\rdata: "text": "Answer"\rdata: }\r\r'
                b'event: done\rdata: {}\r\r')
        self.assertEqual(parse([body]).complete_ms, 1000)

    def test_first_answer_and_complete_have_separate_clocks(self):
        times = iter([3.0, 8.0])
        body = (b": heartbeat\n\n" + frame({"type": "phase", "phase": "searching"})
                + frame({"type": "chunk", "text": " \n"})
                + frame({"type": "chunk", "text": "Answer"}) + frame({"type": "done"}))
        metrics = parse([body], lambda: next(times))
        self.assertEqual(metrics.first_token_ms, 3000)
        self.assertEqual(metrics.complete_ms, 8000)

    def test_error_payloads_are_not_reported(self):
        private = secrets.token_urlsafe(16)
        for body in (frame({"type": "error", "text": private}),
                     ("event: error\ndata: " + private + "\n\n").encode()):
            with self.assertRaisesRegex(loadtest.LoadTestError, "^sse_error$"):
                parse([body])

    def test_partial_answer_then_error_is_failure(self):
        metrics = loadtest.StreamMetrics()
        with self.assertRaisesRegex(loadtest.LoadTestError, "^sse_error$"):
            loadtest.read_sse([frame({"type": "chunk", "text": "Partial"}),
                               frame({"type": "error", "text": "synthetic"})],
                              metrics, 0, lambda: 2)
        self.assertEqual(metrics.first_token_ms, 2000)
        self.assertIsNone(metrics.complete_ms)

    def test_headers_phases_heartbeats_and_partial_done_are_not_completion(self):
        for body in (b"", b": heartbeat\n\n", frame({"type": "phase"}),
                     frame({"type": "chunk", "text": "Partial"}),
                     frame({"type": "chunk", "text": "Partial"}) + b'data: {"type":"done"}\n'):
            with (self.subTest(body_length=len(body)),
                  self.assertRaisesRegex(loadtest.LoadTestError, "^missing_done$")):
                parse([body])

    def test_empty_answer_fails_even_with_done(self):
        for prefix in (b"", frame({"type": "chunk", "text": "  \n"})):
            with self.assertRaisesRegex(loadtest.LoadTestError, "^empty_answer$"):
                parse([prefix, frame({"type": "done"})])

    def test_invalid_events_fail_without_content(self):
        for body, reason in ((b"data: {invalid\n\n", "invalid_sse_json"),
                             (frame([]), "invalid_sse_payload"),
                             (frame({"type": "chunk", "text": 42}), "invalid_answer_chunk"),
                             (b"data: \xff\n\n", "invalid_sse_encoding")):
            with self.assertRaisesRegex(loadtest.LoadTestError, "^" + reason + "$"):
                parse([body])

    def test_disconnection_is_sanitized(self):
        def broken():
            yield frame({"type": "chunk", "text": "Partial"})
            raise OSError(secrets.token_urlsafe(16))
        with self.assertRaisesRegex(loadtest.LoadTestError, "^stream_disconnected$"):
            parse(broken())

    def test_done_does_not_hide_a_late_transport_error(self):
        def broken():
            yield frame({"type": "chunk", "text": "Public answer"})
            yield frame({"type": "done"})
            raise OSError(secrets.token_urlsafe(16))
        with self.assertRaisesRegex(loadtest.LoadTestError, "^stream_disconnected$"):
            parse(broken())
        body = frame({"type": "chunk", "text": "Public answer"}) + frame({"type": "done"})
        with self.assertRaisesRegex(loadtest.LoadTestError, "^sse_error$"):
            parse([body, frame({"type": "error"})])

    def test_parser_memory_is_bounded(self):
        with (patch.object(loadtest, "MAX_EVENT_BYTES", 10),
              self.assertRaisesRegex(loadtest.LoadTestError, "^sse_event_too_large$")):
            parse([b"data: " + b"x" * 20])
        with (patch.object(loadtest, "MAX_STREAM_BYTES", 10),
              self.assertRaisesRegex(loadtest.LoadTestError, "^sse_stream_too_large$")):
            parse([b": " + b"x" * 20])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ALLOW_LIVE": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="chat-loadtest-", dir=os.environ.get("TMP") or os.environ.get("TEMP"))
        self.addCleanup(self.temp.cleanup)
        self.parser = argparse.ArgumentParser()
        self.parser.add_argument("--host", default="http://127.0.0.1:8765")
        self.parser.add_argument("--headless", action="store_true")
        self.parser.add_argument("-u", dest="num_users", type=int)
        loadtest.add_arguments(self.parser)

    def options(self, *extra):
        return self.parser.parse_args(["--headless", "-u", "30", *extra])

    def json_file(self, name, payload):
        path = Path(self.temp.name) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_default_cost_is_one_turn_each_for_thirty_unique_users(self):
        config = loadtest.settings_from_options(self.options("--chat-guest"))
        self.assertEqual((config.users, config.turns, config.allow_live), (30, 1, False))
        cohort = loadtest.Cohort(config.users)
        indices = [cohort.claim() for _ in range(30)]
        self.assertEqual(len(set(indices)), 30)
        with self.assertRaisesRegex(loadtest.LoadTestError, "^user_budget_exhausted$"):
            cohort.claim()
        for index in indices[:-1]:
            self.assertFalse(cohort.finish(index))
        self.assertFalse(cohort.finish(indices[0]))
        self.assertTrue(cohort.finish(indices[-1]))

    def test_guest_is_explicit_and_exclusive(self):
        for options in (self.options(), self.options("--chat-guest", "--chat-accounts", "unused")):
            with self.assertRaisesRegex(loadtest.LoadTestError, "choose_exactly_one"):
                loadtest.settings_from_options(options)

    def test_remote_and_decorated_targets_are_blocked(self):
        for host in ("https://example.invalid", "http://user:secret@localhost:8765",
                     "http://localhost:8765/?secret=1", "http://localhost:8765/chat",
                     "file:///stub", "http://localhost:bad", "http://localhost:0", ""):
            with self.subTest(host=host), self.assertRaises(loadtest.LoadTestError):
                loadtest.validate_target(host)

    def test_localhost_is_not_itself_authorization(self):
        self.assertEqual(loadtest.validate_target("http://localhost:8765/"), "http://localhost:8765")
        for status, headers, payload in ((200, {}, {}), (302, {}, {}),
                                        (200, {loadtest.STUB_HEADER: loadtest.STUB_MARKER}, {"test_only": True})):
            with self.assertRaisesRegex(loadtest.LoadTestError, "unmarked_target_blocked"):
                loadtest.require_stub(status, headers, payload)
        loadtest.require_stub(200, {loadtest.STUB_HEADER: loadtest.STUB_MARKER},
                              {"stub": loadtest.STUB_MARKER, "test_only": True})

    def test_live_opt_in_is_exact_and_does_not_make_requests(self):
        for value in ("0", "false", "yes", "true", ""):
            with (patch.dict(os.environ, {"ALLOW_LIVE": value}),
                  self.assertRaises(loadtest.LoadTestError)):
                loadtest.settings_from_options(self.options("--chat-guest", "--host", "https://example.invalid"))
        with patch.dict(os.environ, {"ALLOW_LIVE": "1"}):
            config = loadtest.settings_from_options(self.options("--chat-guest", "--host", "https://example.invalid"))
            self.assertTrue(config.allow_live)
        with self.assertRaisesRegex(loadtest.LoadTestError, "live_remote_targets_require_https"):
            loadtest.validate_target("http://example.invalid", allow_live=True)

    def test_followups_need_an_explicit_finite_turn_count(self):
        config = loadtest.settings_from_options(self.options("--chat-guest", "--chat-turns", "2"))
        self.assertEqual(config.turns, 2)
        self.assertGreater(len(config.scenarios), 1)
        for extra in (("--chat-turns", "0"), ("--chat-turns", "3"),
                      ("--chat-timeout", "inf"), ("--chat-timeout", "nan"), ("--chat-timeout", "0")):
            with self.assertRaises(loadtest.LoadTestError):
                loadtest.settings_from_options(self.options("--chat-guest", *extra))
        filename = self.json_file("scenario.json", [["Public question", "Related followup"]])
        config = loadtest.settings_from_options(self.options("--chat-guest", "--chat-turns", "2", "--chat-scenario", filename))
        self.assertEqual(config.scenarios[0][1], "Related followup")

    def test_accounts_are_external_distinct_and_never_recycled(self):
        secret = secrets.token_urlsafe(20)
        rows = [{"email": f"test-{i}@example.invalid", "password": secret} for i in range(30)]
        filename = self.json_file("accounts.json", rows)
        config = loadtest.settings_from_options(self.options("--chat-accounts", filename))
        self.assertEqual(len(config.accounts), 30)
        self.assertNotIn(secret, repr(config))
        self.assertNotIn(rows[0]["email"], repr(config))
        for bad_rows, reason in ((rows[:29], "not_enough_distinct_test_accounts"),
                                 (rows[:-1] + [rows[0]], "duplicate_test_account"),
                                 ([{}] * 30, "invalid_test_account_entry")):
            filename = self.json_file("invalid.json", bad_rows)
            with self.assertRaisesRegex(loadtest.LoadTestError, "^" + reason + "$"):
                loadtest.settings_from_options(self.options("--chat-accounts", filename))
        with self.assertRaisesRegex(loadtest.LoadTestError, "account_file_must_be_outside_workspace"):
            loadtest.settings_from_options(self.options("--chat-accounts", str(TOOL)))

    def test_bad_files_do_not_echo_paths_or_content(self):
        filename = self.json_file(".env", {})
        with self.assertRaisesRegex(loadtest.LoadTestError, "^dotenv_files_are_not_test_inputs$"):
            loadtest.read_json_file(filename, "accounts")
        with self.assertRaisesRegex(loadtest.LoadTestError, "^accounts_file_invalid$"):
            loadtest.read_json_file(str(Path(self.temp.name) / "missing.json"), "accounts")

    def test_web_and_distributed_execution_are_rejected(self):
        for setting in ("headless", "master", "worker", "processes"):
            options = self.options("--chat-guest")
            setattr(options, setting, setting != "headless")
            with self.assertRaisesRegex(loadtest.LoadTestError, "only_local_headless"):
                loadtest.settings_from_options(options)

    def test_no_application_imports_and_python310_syntax(self):
        for filename in (TOOL, Path(__file__), ROOT / "tests" / "fixtures" / "chat_load_stub.py"):
            tree = ast.parse(filename.read_text(encoding="utf-8"), feature_version=(3, 10))
            imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
            self.assertFalse(any(name and name.split(".")[0] in ("sc_assistant", "rag", "aws") for name in imports))


class StopUserError(Exception):
    pass


class RuntimeTests(unittest.TestCase):
    """Run the actual hook/user methods with in-memory transports, not Locust."""

    def setUp(self):
        env_patch = patch.dict(os.environ, {"ALLOW_LIVE": ""})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        registration = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                            and node.name == "_register_locust")
        nodes = [node for node in registration.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
        for node in nodes:
            node.decorator_list = []
        self.runtime = dict(loadtest.__dict__)
        self.runtime.update(
            HttpUser=object, StopUser=StopUserError, LocalRunner=type("LocalRunner", (), {}),
            task=lambda function: function, Event=Mock, EventHook=Mock,
            gevent=types.SimpleNamespace(Timeout=lambda *args: nullcontext(), spawn=Mock()),
            requests=MagicMock())
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(TOOL), "exec"), self.runtime)  # noqa: S102
        options = types.SimpleNamespace(
            host="http://127.0.0.1:8765/", headless=True, num_users=1, chat_guest=True,
            chat_accounts=None, chat_scenario=None, chat_turns=1, chat_timeout=120)
        runner = self.runtime["LocalRunner"]()
        runner.quit = Mock()
        self.environment = types.SimpleNamespace(
            host=options.host, parsed_options=options, runner=runner,
            shape_class=None, user_classes=[self.runtime["ChatLoadUser"]],
            events=types.SimpleNamespace(request=Mock()), process_exit_code=None)
        self.probe = MagicMock()
        self.probe.__enter__.return_value = self.probe
        self.runtime["requests"].Session.return_value = self.probe
        marker = {"stub": loadtest.STUB_MARKER, "test_only": True}
        self.probe.get.return_value = self.response(
            [json.dumps(marker).encode()], headers={loadtest.STUB_HEADER: loadtest.STUB_MARKER})
        self.client = Mock(cookies={"session": "synthetic-cookie"})
        self.client.request_event = self.environment.events.request
        self.user = self.runtime["ChatLoadUser"]()
        self.user.environment = self.environment
        self.user.client = self.client

    def response(self, chunks, status=200, headers=None):
        response = MagicMock(status_code=status, raw=object())
        response.headers = headers if headers is not None else {"Content-Type": "text/event-stream; charset=utf-8"}
        response.iter_content.return_value = iter(chunks)
        response.__enter__.return_value = response

        def exit_response(*args):
            response.close()
            return False

        response.__exit__.side_effect = exit_response
        return response

    def start(self):
        self.runtime["initialize"](self.environment)
        auth = self.response([b'{"success":true}'])

        def post(*args, **kwargs):
            self.client.request_event.fire(request_type="POST", response_time=0)
            return auth

        self.client.post.side_effect = post
        self.user.on_start()
        auth.close.assert_called_once()
        self.client.post.side_effect = None
        return self.user

    def records(self, request_type):
        return [call.kwargs for call in self.environment.events.request.fire.call_args_list
                if call.kwargs["request_type"] == request_type]

    def test_guard_precedes_all_authentication_and_closes_probe(self):
        rejected = self.response([b'{"status":"ready"}'], headers={})
        self.probe.get.return_value = rejected
        with (self.assertLogs(loadtest.LOG, level="ERROR") as logs,
              self.assertRaises(SystemExit) as stopped):
            self.runtime["initialize"](self.environment)
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("unmarked_target_blocked", " ".join(logs.output))
        self.client.post.assert_not_called()
        rejected.close.assert_called_once()
        self.assertFalse(self.probe.trust_env)
        self.assertFalse(self.probe.get.call_args.kwargs["allow_redirects"])

    def test_remote_guard_does_not_make_even_a_probe(self):
        self.environment.host = "https://example.invalid"
        with self.assertLogs(loadtest.LOG, level="ERROR"), self.assertRaises(SystemExit):
            self.runtime["initialize"](self.environment)
        self.probe.get.assert_not_called()
        self.client.post.assert_not_called()

    def test_mixed_loadfiles_cannot_bypass_the_budget(self):
        self.environment.user_classes.append(object)
        with self.assertLogs(loadtest.LOG, level="ERROR"), self.assertRaises(SystemExit):
            self.runtime["initialize"](self.environment)
        self.probe.get.assert_not_called()

    def test_metrics_replace_header_samples_and_requests_are_multipart(self):
        user = self.start()
        answer = self.response([frame({"type": "chunk", "text": "Public answer"}), frame({"type": "done"})])
        self.client.post.return_value = answer
        with self.assertRaises(StopUserError):
            user.questions()
        user.on_stop()
        user.on_stop()
        self.assertFalse(user.failed)
        self.assertFalse(self.client.trust_env)
        self.assertEqual(self.environment.host, "http://127.0.0.1:8765")
        self.assertEqual(len(self.records("AUTH")), 1)
        self.assertEqual(len(self.records("SSE")), 1)
        self.assertEqual(len(self.records("SSE-TTFT")), 1)
        self.assertEqual(self.records("POST"), [])
        self.assertIsNone(self.records("SSE")[0]["exception"])
        self.assertIsNone(self.records("SSE")[0]["response"])
        self.assertEqual(self.records("SSE")[0]["context"], {"user_id": "u0001", "question_id": "q01.t01"})
        self.assertFalse(self.client.post.call_args.kwargs["allow_redirects"])
        self.assertIsNone(self.client.post.call_args.kwargs["files"]["msg"][0])
        self.assertEqual(self.client.post.call_count, 2)  # One login and one question.
        answer.close.assert_called_once()
        self.runtime["gevent"].spawn.assert_called_once_with(self.environment.runner.quit)

    def test_http_and_body_failures_are_closed_and_counted(self):
        user = self.start()
        cases = [(self.response([], status=429), "http_429"),
                 (self.response([], status=500), "http_500"),
                 (self.response([], status=302), "http_302"),
                 (self.response([], headers={"Content-Type": "text/html"}), "not_an_sse_response"),
                 (self.response([frame({"type": "phase"})]), "missing_done"),
                 (self.response([frame({"type": "done"})]), "empty_answer")]
        for response, code in cases:
            with self.subTest(code=code):
                self.client.post.return_value = response
                self.assertFalse(user.ask("Synthetic question", "q01.t01"))
                self.assertEqual(str(self.records("SSE")[-1]["exception"]), code)
                response.close.assert_called_once()
        self.assertEqual(self.records("SSE-TTFT"), [])

    def test_partial_answer_failure_cannot_look_like_ttft_success(self):
        user = self.start()
        secret = secrets.token_urlsafe(16)
        self.client.post.return_value = self.response([
            frame({"type": "chunk", "text": secret}), frame({"type": "error", "text": secret})])
        with self.assertLogs(loadtest.LOG, level="INFO") as logs:
            self.assertFalse(user.ask(secret, "q01.t01"))
        self.assertNotIn(secret, " ".join(logs.output))
        self.assertEqual(str(self.records("SSE")[-1]["exception"]), "sse_error")
        self.assertEqual(str(self.records("SSE-TTFT")[-1]["exception"]), "sse_error")

    def test_unexpected_tool_error_stops_instead_of_repeating(self):
        user = self.start()
        user.ask = Mock(side_effect=ValueError(secrets.token_urlsafe(16)))
        with self.assertLogs(loadtest.LOG, level="ERROR"), self.assertRaises(StopUserError):
            user.questions()
        user.on_stop()
        self.assertEqual(user.ask.call_count, 1)
        self.assertEqual(self.environment.process_exit_code, 1)

    def test_followups_remain_bounded_and_stop_after_failure(self):
        self.environment.parsed_options.chat_turns = 2
        user = self.start()
        user.ask = Mock(side_effect=[True, False])
        with self.assertRaises(StopUserError):
            user.questions()
        self.assertEqual(user.ask.call_count, 2)
        self.assertEqual(user.ask.call_args.args, (loadtest.DEFAULT_SCENARIOS[0][1], "q01.t02"))
        self.assertTrue(user.failed)

    def test_response_close_error_still_records_failure(self):
        user = self.start()
        answer = self.response([frame({"type": "chunk", "text": "Public answer"}), frame({"type": "done"})])
        answer.close.side_effect = OSError(secrets.token_urlsafe(16))
        self.client.post.return_value = answer
        self.assertFalse(user.ask("Synthetic question", "q01.t01"))
        self.assertEqual(str(self.records("SSE")[-1]["exception"]), "response_close_failed")


@unittest.skipUnless(importlib.util.find_spec("flask"), "Flask not installed: skip offline fixture tests only")
class StubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        filename = ROOT / "tests" / "fixtures" / "chat_load_stub.py"
        spec = importlib.util.spec_from_file_location("_chat_load_stub", filename)
        cls.stub = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.stub)

    def app(self, mode="ok"):
        return self.stub.create_app(mode, first_token_delay=0, done_delay=0, chunk_size=1)

    def test_thirty_distinct_cookie_sessions_and_exactly_one_turn(self):
        app = self.app()
        for _ in range(30):
            client = app.test_client()
            auth = client.post("/guest")
            self.assertTrue(auth.json["success"])
            with client.post("/chat/get", data={"msg": "Public fixture question"},
                             content_type="multipart/form-data") as response:
                metrics = parse(response.response)
                self.assertIsNotNone(metrics.complete_ms)
                response.get_data()
        response = app.test_client().get(loadtest.STUB_PATH)
        loadtest.require_stub(response.status_code, response.headers, response.json)
        self.assertEqual(response.json["guest_requests"], 30)
        self.assertEqual(response.json["chat_requests"], 30)
        self.assertEqual(response.json["chat_sessions"], 30)
        self.assertEqual(response.json["max_turns_per_session"], 1)

    def test_stub_login_accepts_only_nonempty_synthetic_form(self):
        client = self.app().test_client()
        self.assertEqual(client.post("/login").status_code, 400)
        response = client.post("/login", data={"email": "test@example.invalid", "password": secrets.token_urlsafe(16)})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])

    def test_fault_modes_and_unmarked_guard(self):
        for mode, code in (("error", "sse_error"), ("missing-done", "missing_done"),
                           ("empty", "empty_answer"), ("disconnect", "stream_disconnected")):
            with self.subTest(mode=mode):
                client = self.app(mode).test_client()
                client.post("/guest")
                with (client.post("/chat/get", data={"msg": "Public fixture question"},
                                  content_type="multipart/form-data") as response,
                      self.assertRaisesRegex(loadtest.LoadTestError, "^" + code + "$")):
                    parse(response.response)
        for mode in ("429", "500"):
            client = self.app(mode).test_client()
            client.post("/guest")
            self.assertEqual(client.post("/chat/get", data={"msg": "Public fixture question"},
                                         content_type="multipart/form-data").status_code, int(mode))
        response = self.app("unmarked").test_client().get(loadtest.STUB_PATH)
        with self.assertRaisesRegex(loadtest.LoadTestError, "unmarked_target_blocked"):
            loadtest.require_stub(response.status_code, response.headers, response.json)

if __name__ == "__main__":
    unittest.main(verbosity=2)
