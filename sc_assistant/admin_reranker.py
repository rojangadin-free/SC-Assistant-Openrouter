"""
sc_assistant/admin_reranker.py — the admin API behind the reranker switch.

Endpoints
---------
GET  /admin/reranker/api/state     which backend is set, which is live, why
POST /admin/reranker/api/set       change it   {"backend": "local" | "api"}

Two fields, not one
-------------------
`state` reports BOTH `backend` (what an admin chose) and `active` (what a question
asked right now would actually use). They differ whenever the choice cannot be
honoured — `api` selected with no `OPENROUTER_API_KEY`, or `local` selected on a
box whose weights were never downloaded — and the router silently uses the other
one so students keep getting ranked answers.

Reporting only the setting would make that fallback invisible, which is the exact
failure this project has already been bitten by twice: `RERANKER_ENABLED=false`
and a missing model produce byte-identical behaviour, and a screen that shows a
green "API" pill while every question is being ranked locally is worse than no
screen at all. The template renders the difference explicitly.

Why POST returns the full state
-------------------------------
The response to a switch is the same payload as `state`, re-read after the write.
The front-end therefore never has to assume the write did what it asked: if an
admin selects `api` without a key, the response comes back
`backend=api, active=local`, and the UI can say so immediately instead of showing
success and letting them discover it in a log.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from .utils import is_admin
from rag import rerank_settings, rerank_router

bp_reranker = Blueprint("reranker", __name__, url_prefix="/admin/reranker")


def _require_admin() -> bool:
    return bool(session.get("user")) and is_admin()


@bp_reranker.route("/api/state")
def api_state():
    """The switch's current position plus both backends' readiness."""
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        return jsonify({"success": True, **rerank_router.describe()})
    except Exception as e:
        # A dashboard panel that cannot read its own state must say so rather
        # than render an empty card that looks like "nothing configured".
        return jsonify({"success": False, "message": str(e)}), 500


@bp_reranker.route("/api/set", methods=["POST"])
def api_set():
    """
    Switch backends. Takes effect on the next question — no restart.

    `changed_by` is recorded from the session rather than the request body: this
    is the one setting that alters how every answer in the system is ordered, and
    a client-supplied name on that audit trail would be worth nothing.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    backend = (data.get("backend") or "").strip().lower()

    if backend not in rerank_settings.BACKENDS:
        return jsonify({
            "success": False,
            "message": (
                f"backend must be one of {list(rerank_settings.BACKENDS)}, "
                f"got {backend!r}"
            ),
        }), 400

    who = ""
    user = session.get("user") or {}
    if isinstance(user, dict):
        who = user.get("email") or user.get("username") or ""

    try:
        rerank_settings.set_backend(backend, changed_by=who)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    # Re-read rather than echo the request: with STORE_BACKEND=dynamodb the write
    # is what other instances will see, and reporting the intent instead of the
    # stored result would hide a failed write behind a success message.
    state = rerank_router.describe()

    message = f"Reranker set to {state['backend'].upper()}."
    if state.get("falling_back"):
        # The switch was saved but cannot be honoured. Said plainly, with the
        # reason, because "saved" and "working" are different claims.
        detail = (state.get(state["backend"], {}) or {}).get("reason", "")
        message = (
            f"Saved, but {state['backend'].upper()} is not usable"
            + (f" ({detail})" if detail else "")
            + f" — questions are being ranked by {state['active'].upper()} instead."
        )
    elif not state.get("active"):
        message = (
            "Saved, but no reranker is usable right now — answers will keep "
            "retrieval order until one is available."
        )

    return jsonify({"success": True, "message": message, **state})
