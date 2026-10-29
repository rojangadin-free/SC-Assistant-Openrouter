"""Finite, opt-in chat load test; never imports the application.

Offline transport/tool validation (NOT production capacity proof)::

    python tests/fixtures/chat_load_stub.py --port 8765
    python -m locust -f tools/loadtest_chat.py --headless -u 30 -r 30 \
        --host http://127.0.0.1:8765 --chat-guest

Real targets require ALLOW_LIVE=1, separate authorization, and an explicit host.
Use --chat-accounts with an EXTERNAL JSON list of {"email": ..., "password": ...}
for pre-existing, distinct test accounts. Never use real student accounts.
No accounts are created. Proxies, .netrc authentication, and redirects are off.

Default: one question per user, then automatic exit. --chat-turns 2 enables the
paired followups (60 turns for 30 users). --chat-scenario accepts an external JSON
list of question sequences, e.g. [["Public question", "Related followup"]].
All sequences must contain at least --chat-turns entries; nothing is cycled.

Use the SSE /chat/get complete row for answer throughput/latency, NOT Aggregate:
AUTH and SSE-TTFT are separate measurements, not additional completed answers.
TTFT is sampled only when non-whitespace answer text arrives; a later error still
fails the answer. Logs contain generated record IDs and timings, never payloads.
Keep any Locust CSV/log/report outputs in an approved TEMP/TMP directory.
"""

import codecs
import ipaddress
import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter, time
from urllib.parse import urlsplit

STUB_PATH = "/__loadtest__/health"
STUB_MARKER = "sc-assistant-chat-load-stub-v1"
STUB_HEADER = "X-SC-Loadtest-Stub"
MAX_EVENT_BYTES = 256 * 1024
MAX_STREAM_BYTES = 4 * 1024 * 1024
LOG = logging.getLogger("chat_loadtest")
DEFAULT_SCENARIOS = (
    ("What are the enrollment requirements for new students?",
     "What about for transferees?"),
    ("What are the enrolement requirments at Samar College?",
     "Which of those documents need original copies?"),
    ("What scholrships are available at Samar College?",
     "How can a new student apply for those?"),
    ("What are the library hours and borrowing rules?",
     "Can those books be borrowed over the weekend?"),
    ("What are the criteria for latin honir awards?",
     "Do those criteria include transferred grades?"),
    ("Which undergraduate programs does Samar College offer?",
     "What about senior high school tracks?"),
)


class LoadTestError(Exception):
    """Only fixed, non-sensitive error codes/messages belong in this exception."""


@dataclass
class StreamMetrics:
    response_length: int = 0
    first_token_ms: float | None = None
    complete_ms: float | None = None


def iter_sse_events(chunks):
    """Parse UTF-8 SSE across arbitrary byte, line, and CRLF boundaries.

    A trailing event without its blank-line delimiter is deliberately discarded:
    a disconnected stream must not turn an incomplete done frame into success.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    line, data = [], []
    event = "message"
    skip_lf = False
    event_size = 0
    try:
        for chunk in chunks:
            for char in decoder.decode(chunk):
                if skip_lf:
                    skip_lf = False
                    if char == "\n":
                        continue
                if char not in "\r\n":
                    line.append(char)
                    event_size += 1
                    if event_size > MAX_EVENT_BYTES:
                        raise LoadTestError("sse_event_too_large")
                    continue
                skip_lf = char == "\r"
                text = "".join(line)
                line.clear()
                if not text:
                    if data:
                        yield event, "\n".join(data)
                    data.clear()
                    event = "message"
                    event_size = 0
                elif not text.startswith(":"):
                    name, _, value = text.partition(":")
                    value = value.removeprefix(" ")
                    if name == "data":
                        data.append(value)
                    elif name == "event":
                        event = value or "message"
        decoder.decode(b"", final=True)
    except UnicodeError:
        raise LoadTestError("invalid_sse_encoding") from None


def read_sse(chunks, metrics, started, clock=perf_counter):
    """Consume the complete body, requiring done and no interrupted transport."""
    def counted_chunks():
        for chunk in chunks:
            metrics.response_length += len(chunk)
            if metrics.response_length > MAX_STREAM_BYTES:
                raise LoadTestError("sse_stream_too_large")
            yield chunk

    done = False
    try:
        for event, data in iter_sse_events(counted_chunks()):
            if event == "error":
                raise LoadTestError("sse_error")
            try:
                payload = json.loads(data)
            except (ValueError, RecursionError):
                raise LoadTestError("invalid_sse_json") from None
            if not isinstance(payload, dict):
                raise LoadTestError("invalid_sse_payload")
            kind = payload.get("type", event)
            if kind == "error":
                raise LoadTestError("sse_error")
            if done:
                raise LoadTestError("event_after_done")
            if kind == "chunk":
                text = payload.get("text")
                if not isinstance(text, str):
                    raise LoadTestError("invalid_answer_chunk")
                if text.strip() and metrics.first_token_ms is None:
                    metrics.first_token_ms = (clock() - started) * 1000
            elif kind == "done":
                if metrics.first_token_ms is None:
                    raise LoadTestError("empty_answer")
                done = True
    except LoadTestError:
        raise
    except Exception:  # noqa: BLE001 - transport exceptions can contain response data
        raise LoadTestError("stream_disconnected") from None
    if not done:
        raise LoadTestError("missing_done")
    metrics.complete_ms = (clock() - started) * 1000


def validate_target(host, allow_live=False):
    """Validate before any network or account-file access; never infer consent."""
    try:
        parsed = urlsplit(host or "")
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise LoadTestError("invalid_host") from None
    if (parsed.scheme not in ("http", "https") or not hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
            or any(char.isspace() for char in host)):
        raise LoadTestError("host_must_be_an_origin_without_credentials_or_query")
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.lower() == "localhost"
    if not allow_live and not loopback:
        raise LoadTestError("offline_mode_requires_loopback_and_stub_marker; ALLOW_LIVE=1_is_required_for_live")
    if allow_live and not loopback and parsed.scheme != "https":
        raise LoadTestError("live_remote_targets_require_https")
    return host.rstrip("/")


def require_stub(status, headers, payload):
    if (status != 200 or headers.get(STUB_HEADER) != STUB_MARKER
            or not isinstance(payload, dict) or payload.get("stub") != STUB_MARKER
            or payload.get("test_only") is not True):
        raise LoadTestError("unmarked_target_blocked; ALLOW_LIVE=1_is_required_for_live")


def add_arguments(parser):
    parser.add_argument("--chat-guest", action="store_true",
                        help="Explicitly use /guest instead of pre-existing test accounts.")
    parser.add_argument("--chat-accounts", default=None,
                        help="External JSON test-account list; never put passwords on the CLI.")
    parser.add_argument("--chat-scenario", default=None,
                        help="JSON list of question sequences; logs use generated IDs only.")
    parser.add_argument("--chat-turns", type=int, default=1,
                        help="Finite questions per user; default 1, paired followups use 2.")
    parser.add_argument("--chat-timeout", type=float, default=120.0,
                        help="Hard total timeout per chat in seconds, including heartbeats.")


def read_json_file(filename, label, external=False):
    try:
        path = Path(filename).resolve()
        if external and path.is_relative_to(Path(__file__).resolve().parent.parent):
            raise LoadTestError("account_file_must_be_outside_workspace")
        if path.name == ".env" or path.name.startswith(".env."):
            raise LoadTestError("dotenv_files_are_not_test_inputs")
        if path.stat().st_size > 1024 * 1024:
            raise LoadTestError(label + "_file_too_large")
        return json.loads(path.read_text(encoding="utf-8"))
    except LoadTestError:
        raise
    except (OSError, ValueError, RecursionError):
        raise LoadTestError(label + "_file_invalid") from None


@dataclass
class Settings:
    host: str
    users: int
    turns: int
    timeout: float
    guest: bool
    allow_live: bool
    accounts: tuple = field(repr=False)
    scenarios: tuple = field(repr=False)


def settings_from_options(options):
    allow_live = os.environ.get("ALLOW_LIVE") == "1"
    host = validate_target(getattr(options, "host", None), allow_live)
    if (not getattr(options, "headless", False)
            or getattr(options, "master", False) or getattr(options, "worker", False)
            or getattr(options, "processes", None)):
        raise LoadTestError("only_local_headless_finite_cohorts_are_supported")
    users = getattr(options, "num_users", None)
    if not isinstance(users, int) or users < 1:
        raise LoadTestError("provide_a_positive_user_count_with_-u")
    if options.chat_turns < 1 or not math.isfinite(options.chat_timeout) or options.chat_timeout <= 0:
        raise LoadTestError("chat_turns_and_timeout_must_be_positive_and_finite")
    if bool(options.chat_guest) == bool(options.chat_accounts):
        raise LoadTestError("choose_exactly_one_of_--chat-guest_or_--chat-accounts")
    accounts = ()
    if options.chat_accounts:
        rows = read_json_file(options.chat_accounts, "accounts", external=True)
        if not isinstance(rows, list) or len(rows) < users:
            raise LoadTestError("not_enough_distinct_test_accounts")
        seen, accounts = set(), []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("email"), str)
                    or not row["email"].strip() or not isinstance(row.get("password"), str)
                    or not row["password"]):
                raise LoadTestError("invalid_test_account_entry")
            identifier = row["email"].strip()
            if identifier.casefold() in seen:
                raise LoadTestError("duplicate_test_account")
            seen.add(identifier.casefold())
            accounts.append((identifier, row["password"]))
    scenarios = (read_json_file(options.chat_scenario, "scenario")
                 if options.chat_scenario else DEFAULT_SCENARIOS)
    if not isinstance(scenarios, (list, tuple)) or not scenarios:
        raise LoadTestError("scenario_must_be_a_nonempty_list_of_question_sequences")
    for sequence in scenarios:
        if (not isinstance(sequence, (list, tuple)) or len(sequence) < options.chat_turns
                or any(not isinstance(q, str) or not q.strip() or len(q) > 8000 for q in sequence)):
            raise LoadTestError("each_scenario_needs_enough_nonempty_bounded_questions")
    return Settings(host, users, options.chat_turns, options.chat_timeout,
                    options.chat_guest, allow_live, tuple(accounts), tuple(map(tuple, scenarios)))


class Cohort:
    """An allocation cap, not a continuously replenished user/account pool."""

    def __init__(self, limit):
        self.limit = limit
        self.claimed = 0
        self.finished = set()

    def claim(self):
        if self.claimed >= self.limit:
            raise LoadTestError("user_budget_exhausted")
        index = self.claimed
        self.claimed += 1
        return index

    def finish(self, index):
        if index in self.finished:
            return False
        self.finished.add(index)
        return len(self.finished) == self.limit


def close_response(response):
    """Dummy connection-failure responses have no raw body to close."""
    try:
        if response is not None and response.raw is not None:
            response.close()
    except Exception:  # noqa: BLE001 - cleanup must not skip failure metrics or stop handling
        return False
    return True


def small_json(response):
    body = bytearray()
    for chunk in response.iter_content(chunk_size=4096):
        body.extend(chunk)
        if len(body) > 8192:
            raise LoadTestError("control_response_too_large")
    try:
        return json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise LoadTestError("invalid_control_response") from None


def _register_locust():
    try:
        import gevent
        import requests
        from gevent.event import Event
        from locust import HttpUser, events, task
        from locust.event import EventHook
        from locust.exception import StopUser
        from locust.runners import LocalRunner
    except ImportError:
        raise SystemExit("Locust is required: install it in a separate tool environment, then use python -m locust.") from None

    events.init_command_line_parser.add_listener(add_arguments)

    @events.init.add_listener
    def initialize(environment, **kwargs):
        try:
            options = environment.parsed_options
            options.host = environment.host or options.host
            config = settings_from_options(options)
            if not isinstance(environment.runner, LocalRunner) or environment.shape_class is not None:
                raise LoadTestError("distributed_workers_and_load_shapes_are_not_supported")
            if environment.user_classes != [ChatLoadUser]:
                raise LoadTestError("run_this_locustfile_alone_to_preserve_the_cohort_budget")
            if not config.allow_live:
                with requests.Session() as probe:
                    probe.trust_env = False
                    with (gevent.Timeout(5, LoadTestError("stub_probe_timeout")),
                          probe.get(config.host + STUB_PATH, stream=True,
                                    timeout=(3, 3), allow_redirects=False) as response):
                        require_stub(response.status_code, response.headers, small_json(response))
            environment.host = config.host
            environment.chat_config = config
            environment.chat_cohort = Cohort(config.users)
            environment.chat_gate = Event()
            environment.chat_ready = 0
            environment.chat_auth_failed = False
        except Exception as exc:  # noqa: BLE001 - never expose network/JSON exception details
            code = str(exc) if isinstance(exc, LoadTestError) else "stub_probe_failed"
            LOG.error("chat_loadtest_blocked: %s", code)
            raise SystemExit(2) from None
        LOG.warning("%s; users=%d turns_per_user=%d max_chat_requests=%d",
                    "LIVE OPT-IN" if config.allow_live else "STUB: transport/tool validation, NOT production capacity proof",
                    config.users, config.turns, config.users * config.turns)
        LOG.info("Answer metrics: SSE complete; TTFT and AUTH are separate, Aggregate is not answer throughput.")

    @events.quitting.add_listener
    def verify_finished(environment, **kwargs):
        cohort = getattr(environment, "chat_cohort", None)
        if cohort is not None and len(cohort.finished) != cohort.limit:
            environment.process_exit_code = 1
            LOG.error("chat_loadtest_incomplete_cohort")

    class ChatLoadUser(HttpUser):
        def on_start(self):
            self.failed = True
            self.index = None
            self.config = self.environment.chat_config
            # Suppress HttpSession's automatic header-only samples. All recorded
            # measurements below are explicit AUTH/full-SSE/first-answer-token.
            self.client.request_event = EventHook()
            self.client.trust_env = False
            try:
                self.index = self.environment.chat_cohort.claim()
            except LoadTestError:
                self.environment.process_exit_code = 1
                raise StopUser() from None
            self.record_id = f"u{self.index + 1:04d}"
            self.sequence_index = self.index % len(self.config.scenarios)
            path = "/guest" if self.config.guest else "/login"
            data = None
            if not self.config.guest:
                identifier, password = self.config.accounts[self.index]
                data = {"email": identifier, "password": password}
            started, response, failure = perf_counter(), None, "auth_interrupted"
            start_time = time()
            try:
                timeout = min(15, self.config.timeout)
                with gevent.Timeout(timeout, LoadTestError("auth_timeout")):
                    response = self.client.post(path, data=data, stream=True,
                                                allow_redirects=False, timeout=(min(5, timeout), timeout))
                    if response.status_code != 200:
                        raise LoadTestError("auth_http_failure")
                    payload = small_json(response)
                    if (not isinstance(payload, dict) or payload.get("success") is not True
                            or not self.client.cookies):
                        raise LoadTestError("auth_rejected_or_cookie_missing")
                    failure = None
            except Exception as exc:  # noqa: BLE001 - credentials must never reach exception output
                failure = str(exc) if isinstance(exc, LoadTestError) else "auth_transport_failure"
            finally:
                if not close_response(response):
                    failure = failure or "response_close_failed"
                self.environment.events.request.fire(
                    request_type="AUTH", name=path, response_time=(perf_counter() - started) * 1000,
                    response_length=0, exception=LoadTestError(failure) if failure else None,
                    response=None, start_time=start_time, context={"user_id": self.record_id})
            if failure:
                self.environment.chat_auth_failed = True
                self.environment.chat_gate.set()
                raise StopUser()
            self.environment.chat_ready += 1
            if self.environment.chat_ready == self.config.users:
                self.environment.chat_gate.set()
            # A cohort barrier makes -u 30 -r 30 a burst after login, rather than
            # allowing fast stub answers to finish before the last user spawns.
            self.environment.chat_gate.wait()
            if self.environment.chat_auth_failed:
                raise StopUser()

        @task
        def questions(self):
            try:
                sequence = self.config.scenarios[self.sequence_index]
                for turn in range(self.config.turns):
                    if not self.ask(sequence[turn], f"q{self.sequence_index + 1:02d}.t{turn + 1:02d}"):
                        break
                else:
                    self.failed = False
            except Exception:  # noqa: BLE001 - never let Locust retry a task after a tool error
                LOG.error("chat_tool_failure user_id=%s", self.record_id)
            raise StopUser()

        def ask(self, question, question_id):
            started, response, failure = perf_counter(), None, "stream_interrupted"
            start_time = time()
            metrics = StreamMetrics()
            try:
                with gevent.Timeout(self.config.timeout, LoadTestError("chat_timeout")):
                    response = self.client.post(
                        "/chat/get", files={"msg": (None, question)}, stream=True,
                        headers={"Accept": "text/event-stream"}, allow_redirects=False,
                        timeout=(min(5, self.config.timeout), self.config.timeout))
                    if response.status_code != 200:
                        status = response.status_code
                        raise LoadTestError(f"http_{status}" if status else "transport_failure")
                    if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                        raise LoadTestError("not_an_sse_response")
                    # Small reads avoid requests' default iter_lines buffering
                    # delaying tiny answer frames until the entire body arrives.
                    read_sse(response.iter_content(chunk_size=1), metrics, started)
                    failure = None
            except Exception as exc:  # noqa: BLE001 - stream exceptions can contain answer data
                failure = str(exc) if isinstance(exc, LoadTestError) else "transport_failure"
            finally:
                if not close_response(response):
                    failure = failure or "response_close_failed"
                elapsed = metrics.complete_ms if metrics.complete_ms is not None else (perf_counter() - started) * 1000
                context = {"user_id": self.record_id, "question_id": question_id}
                error = LoadTestError(failure) if failure else None
                self.environment.events.request.fire(
                    request_type="SSE", name="/chat/get complete", response_time=elapsed,
                    response_length=metrics.response_length, exception=error,
                    response=None, start_time=start_time, context=context)
                if metrics.first_token_ms is not None:
                    self.environment.events.request.fire(
                        request_type="SSE-TTFT", name="/chat/get first-answer-token",
                        response_time=metrics.first_token_ms, response_length=0,
                        exception=error, response=None, start_time=start_time, context=context)
                LOG.info("chat_result user_id=%s question_id=%s status=%s complete_ms=%.1f first_token_ms=%s",
                         self.record_id, question_id, failure or "complete", elapsed,
                         "none" if metrics.first_token_ms is None else f"{metrics.first_token_ms:.1f}")
            return failure is None

        def on_stop(self):
            try:
                self.client.close()
            except Exception:  # noqa: BLE001 - still finish the bounded cohort on cleanup failure
                self.failed = True
            if self.failed:
                self.environment.process_exit_code = 1
            if self.index is not None and self.environment.chat_cohort.finish(self.index):
                gevent.spawn(self.environment.runner.quit)

    globals()["ChatLoadUser"] = ChatLoadUser


if __name__ == "__main__":
    raise SystemExit("Use python -m locust -f tools/loadtest_chat.py --headless -u 30 -r 30 --host <origin> --chat-guest")
else:
    _register_locust()
