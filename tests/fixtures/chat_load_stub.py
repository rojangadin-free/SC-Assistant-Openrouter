"""Isolated SSE transport fixture, NOT an application or capacity benchmark.

    python tests/fixtures/chat_load_stub.py --port 8765 --mode ok

No real app, credentials, .env, AWS, Pinecone, models, or persistent user data.
Login accepts any nonempty synthetic pair and creates only an ephemeral cookie.
The health marker exposes counts only, including distinct chat sessions.
"""

import argparse
import json
import logging
import math
import secrets
import threading
import time
import uuid

try:
    from flask import Flask, Response, jsonify, request, session
except ImportError:
    raise SystemExit("The offline stub requires Flask; install it in the isolated tool environment.") from None


STUB_MARKER = "sc-assistant-chat-load-stub-v1"
MODES = ("ok", "error", "missing-done", "empty", "disconnect", "429", "500", "auth-error", "unmarked")


def create_app(mode="ok", first_token_delay=0.05, done_delay=0.10, chunk_size=0):
    if mode not in MODES:
        raise ValueError("invalid_stub_mode")
    if any(not math.isfinite(n) or n < 0 for n in (first_token_delay, done_delay)) or chunk_size < 0:
        raise ValueError("invalid_stub_delay_or_chunk_size")
    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(TESTING=False, MAX_CONTENT_LENGTH=64 * 1024)
    lock = threading.Lock()
    counts = {"login_requests": 0, "guest_requests": 0, "chat_requests": 0,
              "active": 0, "max_active": 0, "completed": 0}
    session_turns = {}

    @app.get("/__loadtest__/health")
    def health():
        with lock:
            payload = dict(counts, chat_sessions=len(session_turns),
                           max_turns_per_session=max(session_turns.values(), default=0))
        if mode == "unmarked":
            return jsonify(payload)
        payload.update(stub=STUB_MARKER, test_only=True,
                       scope="transport/tool validation only; NOT production capacity proof")
        response = jsonify(payload)
        response.headers["X-SC-Loadtest-Stub"] = STUB_MARKER
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/login")
    @app.post("/guest")
    def login():
        with lock:
            counts["guest_requests" if request.path == "/guest" else "login_requests"] += 1
        if mode == "auth-error":
            return jsonify(success=False), 401
        if request.path == "/login" and not (request.form.get("email") and request.form.get("password")):
            return jsonify(success=False), 400
        session.clear()
        session["loadtest_sid"] = uuid.uuid4().hex
        return jsonify(success=True, redirect="/chat/")

    @app.post("/chat/get")
    def chat():
        sid = session.get("loadtest_sid")
        if not sid:
            return jsonify(error="test_session_required"), 401
        if (request.mimetype != "multipart/form-data" or not request.form.get("msg", "").strip()
                or request.files):
            return jsonify(error="multipart_text_msg_required"), 400
        with lock:
            counts["chat_requests"] += 1
            session_turns[sid] = session_turns.get(sid, 0) + 1
        if mode in ("429", "500"):
            return jsonify(error="stub_http_failure"), int(mode)

        def frames():
            with lock:
                counts["active"] += 1
                counts["max_active"] = max(counts["max_active"], counts["active"])
            try:
                yield b": transport fixture\r\n\r\n"
                yield b'data: {"type":"phase","phase":"searching"}\r\n\r\n'
                time.sleep(first_token_delay)
                yield b": heartbeat\r\n\r\n"
                if mode == "error":
                    yield b'data: {"type":"error","text":"stub_failure"}\r\n\r\n'
                    return
                if mode != "empty":
                    # Deliberately multiline JSON, unlike a line-at-a-time parser.
                    yield b'data: {"type":"chunk",\r\ndata: "text":"Public test "}\r\n\r\n'
                    if mode == "disconnect":
                        raise ConnectionResetError("stub_disconnect")
                    yield b'data: {"type":"chunk","text":"answer."}\r\n\r\n'
                time.sleep(done_delay)
                if mode == "missing-done":
                    return
                yield ("data: " + json.dumps({"type": "done", "msg_id": "stub-message"}) + "\r\n\r\n").encode()
                with lock:
                    counts["completed"] += 1
            finally:
                with lock:
                    counts["active"] -= 1

        def body():
            for frame in frames():
                if chunk_size:
                    for offset in range(0, len(frame), chunk_size):
                        yield frame[offset:offset + chunk_size]
                else:
                    yield frame

        return Response(body(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache, no-store, no-transform", "X-Accel-Buffering": "no"})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", choices=MODES, default="ok")
    parser.add_argument("--first-token-delay", type=float, default=0.05)
    parser.add_argument("--done-delay", type=float, default=0.10)
    parser.add_argument("--chunk-size", type=int, default=0, help="Split SSE frames into writes of this size.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        app = create_app(args.mode, args.first_token_delay, args.done_delay, args.chunk_size)
    except ValueError:
        parser.error("delays and chunk size must be finite and nonnegative")
    logging.getLogger("werkzeug").disabled = True
    print("CHAT LOAD STUB: transport/tool validation only; NOT production capacity proof.", flush=True)
    app.run(host=args.host, port=args.port, threaded=True, debug=False,
            use_reloader=False, load_dotenv=False)


if __name__ == "__main__":
    main()
