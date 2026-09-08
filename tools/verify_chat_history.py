"""Verify /chat/conversations end-to-end, without a browser or a real password.

The sidebar's "Failed to load chats" came from `list_conversations()` raising
ResourceNotFoundException — the Conversations table did not exist in
ap-southeast-1. Checking the fix through the UI needs a signed-in session, so
this drives the Flask test client with the session cookie set directly, which is
the same code path `chat.js` hits.

The chat blueprint is mounted on a bare Flask app rather than the real
`create_app()`, for the same reason `tests/test_gaps_api.py` does: the factory
imports the whole RAG stack (LangGraph, Pinecone) and cannot even be imported
without those optional deps installed. `/chat/conversations` touches none of it.

Asserts three things the earlier bug would each have broken:

  1. the route answers 200 with JSON (it was a 500 before)
  2. a stored conversation comes back through the uid-index GSI
  3. a guest gets [] rather than someone else's history

Cleans up the row it writes, so it is safe to run against the live table.

    python tools/verify_chat_history.py
"""

import _bootstrap  # noqa: F401  (repo root on sys.path; must precede repo imports)

import datetime
import sys
import types
import uuid

from aws.dynamodb import delete_conversation_from_db, upsert_conversation

# Stand in for the two heavyweight modules `sc_assistant.chat` imports at module
# scope — `rag.chain` (LangGraph) and `src.helper` (HuggingFace embeddings). The
# route under test calls nothing from either, so stubbing them lets this run
# without the full ML dependency tree installed. If the real modules are already
# importable, nothing is replaced.
def _stub(name, **attrs):
    try:
        __import__(name)
        return
    except Exception:  # noqa: BLE001 - any import failure means we need the stub
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod


_stub("rag.chain", app_graph=None, stream_answer=None)
_stub("src.helper", encode_image=lambda *a, **kw: "")


from flask import Flask  # noqa: E402
from sc_assistant.chat import bp as chat_bp  # noqa: E402

PROBE_UID = "verify-chat-history-probe"


passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  -> {detail}" if detail else ""))


def main():
    global failed

    app = Flask(__name__)
    app.secret_key = "verify-only"
    app.config["TESTING"] = True
    app.register_blueprint(chat_bp)


    conv_id = f"probe-{uuid.uuid4().hex[:8]}"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        upsert_conversation(
            PROBE_UID,
            conv_id,
            [{"role": "user", "content": "does the sidebar load?"}],
            now,
        )

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "probe@example.com"
                sess["uid"] = PROBE_UID
                sess["is_guest"] = False

            resp = client.get("/chat/conversations")
            check("route answers 200", resp.status_code == 200, resp.status_code)

            body = resp.get_json()
            check("returns a JSON list", isinstance(body, list), type(body).__name__)

            ids = [c.get("conv_id") for c in (body or [])]
            check("the stored conversation is listed", conv_id in ids, ids)

            row = next((c for c in (body or []) if c.get("conv_id") == conv_id), {})
            check(
                "title derived from the first user message",
                row.get("title") == "does the sidebar load?",
                row.get("title"),
            )

        # A guest must not inherit history. `_is_guest()` short-circuits before
        # the query, so this also proves the guard survives the table existing.
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "guest"
                sess["is_guest"] = True

            resp = client.get("/chat/conversations")
            check("guest gets an empty list", resp.get_json() == [], resp.get_json())

    finally:
        try:
            delete_conversation_from_db(PROBE_UID, conv_id)
        except Exception as e:  # noqa: BLE001 - cleanup must not mask a failure
            print(f"  note: cleanup failed: {e}")

    print(f"\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
