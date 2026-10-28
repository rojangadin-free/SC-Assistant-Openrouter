"""Fake HTTP/SSE only: no live students, model calls, or cloud writes."""

import _bootstrap  # noqa: F401
import contextlib
import io
import json
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from tools import loadtest_chat


class Handler(BaseHTTPRequestHandler):
    sessions = set()
    lock = threading.Lock()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        if self.path == "/guest":
            body = b'{"success":true}'
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=" + uuid.uuid4().hex + "; Path=/")
        else:
            with self.lock:
                self.sessions.add(self.headers.get("Cookie", ""))
            body = b': heartbeat\n\ndata: {"type":"phase","phase":"searching"}\n\ndata: {"type":"chunk","text":"School answer"}\n\ndata: {"type":"done"}\n\n'
            self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for offset in range(0, len(body), 7):
            self.wfile.write(body[offset:offset + 7])
            self.wfile.flush()

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    request_queue_size = 64


class LoadTests(unittest.TestCase):
    def test_dry_run_does_not_open_connections_or_credentials(self):
        with patch.object(loadtest_chat, "run") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(loadtest_chat.main(["--users", "30", "--session-cookies", "must-not-be-opened.json"]), 0)
        run.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["dry_run"])

    def test_frames_comments_and_multiline_data(self):
        response = io.BytesIO(b': padding\r\n\r\ndata: {"type":\r\ndata: "done"}\r\n\r\n')
        self.assertEqual(list(loadtest_chat.events(response)), [{"type": "done"}])

    def test_error_blank_and_incomplete_answers_are_failures(self):
        for data in (b'data: {"type":"error","text":"failed"}\n\n',
                     b'data: {"type":"done"}\n\n',
                     b'data: {"type":"chunk","text":"partial"}\n\n'):
            with self.assertRaises(RuntimeError):
                loadtest_chat.consume(io.BytesIO(data), __import__("time").perf_counter(), 1)

    def test_30_distinct_sessions_complete_against_fake_sse(self):
        Handler.sessions = set()
        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            args = SimpleNamespace(users=30, requests_per_user=1, query="generic campus question", timeout=10,
                                   base_url="http://127.0.0.1:" + str(server.server_port), session_cookies=None)
            report = loadtest_chat.run(args)
            self.assertEqual(report["completed"], 30, report)
            self.assertEqual(report["failed"], 0)
            self.assertEqual(len(Handler.sessions), 30)
            self.assertNotIn("", Handler.sessions)
            self.assertGreaterEqual(report["completion_p95"], report["ttft_p95"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
