from flask import (
    Blueprint, render_template, jsonify, request, session, redirect, url_for
)
import datetime
import uuid
import os
from PIL import Image
from rag.chain import app_graph
from aws.dynamodb import (
    upsert_conversation, list_conversations,
    get_conversation, delete_conversation_from_db,
    save_report
)
from .utils import get_session_id, is_admin
from src.helper import encode_image

bp = Blueprint('chat', __name__, url_prefix='/chat')

def _is_guest():
    """Returns True if the current session belongs to a guest user."""
    return session.get("is_guest", False) or session.get("user") == "guest"


@bp.route("/")
def chat_page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    start_new = session.pop('start_new_chat', False)
    user_obj = {
        "email":    session.get("user"),
        "username": session.get("username", session.get("user", "").split("@")[0]),
        "is_guest": _is_guest(),
    }
    return render_template("chat.html", user=user_obj, start_new=str(start_new).lower())


@bp.route("/get", methods=["POST"])
def chat():
    if not session.get("user"):
        return jsonify({"error": "Please log in to use the chatbot."}), 401
    try:
        msg = request.form.get("msg", "")
        guest = _is_guest()

        # Image handling
        image_data = None
        image_mime = "image/jpeg"
        if 'image' in request.files and request.files['image'].filename != '':
            try:
                uploaded = request.files['image']
                img = Image.open(uploaded)
                fmt = (img.format or "JPEG").upper()
                image_mime = "image/png" if fmt == "PNG" else "image/jpeg"
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                    image_mime = "image/jpeg"
                image_data = encode_image(img)
            except Exception as img_err:
                print(f"Error processing upload: {img_err}")

        session_id = get_session_id()
        config = {"configurable": {"thread_id": session_id}}

        conv_id = None if guest else session.get("current_conv_id")
        current_db_history = []

        if not guest and conv_id:
            conversation = get_conversation(session.get("uid"), conv_id)
            if conversation and "messages" in conversation:
                current_db_history = conversation["messages"]
                app_graph.update_state(config, values={"chat_history": current_db_history})

        input_payload = {
            "input":      msg,
            "image_data": image_data if image_data else None,
            "image_mime": image_mime  if image_data else None,
            "uid":        None if guest else session.get("uid"),
            "user_email": None if guest else session.get("user"),
        }

        result = app_graph.invoke(input_payload, config=config)
        answer = result.get("answer", "Sorry, I encountered an issue.")

        if guest:
            return jsonify({
                "answer":                   answer,
                "conv_id":                  None,
                "new_conversation_created": False,
                "new_conv_title":           None,
            })

        new_conversation_created = False
        title = None
        if not conv_id:
            conv_id    = str(uuid.uuid4())
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            session["current_conv_id"] = conv_id
            session["created_at"]      = created_at
            new_conversation_created   = True
            title = msg[:40] if msg else "Image Query"
        else:
            created_at = session.get("created_at")

        new_messages = [
            {"role": "user",      "content": msg + (" [Image Uploaded]" if image_data else "")},
            {"role": "assistant", "content": answer},
        ]
        full_history_to_save = current_db_history + new_messages
        
        if session.get("uid"):
            upsert_conversation(session.get("uid"), conv_id, full_history_to_save, created_at)

        return jsonify({
            "answer":                   answer,
            "conv_id":                  conv_id,
            "new_conversation_created": new_conversation_created,
            "new_conv_title":           title,
        })

    except Exception as e:
        print(f"Error in /get endpoint: {e}")
        return jsonify({"answer": f"Sorry, an error occurred: {str(e)}"}), 500


@bp.route("/report", methods=["POST"])
def submit_report():
    if not session.get("user"):
        return jsonify({"error": "Not authenticated"}), 401
    if _is_guest():
        return jsonify({"error": "Guests cannot submit reports. Please log in."}), 403
    try:
        data        = request.get_json()
        msg_snippet = data.get("msg_snippet", "")
        reason      = data.get("reason", "")
        other_text  = data.get("other_text", "")
        conv_id     = data.get("conv_id")
        msg_id      = data.get("msg_id", "")

        save_report({
            "reporter_email": session.get("user"),
            "reporter_uid":   session.get("uid"),
            "conv_id":        conv_id,
            "msg_id":         msg_id,
            "reason":         reason,
            "other_text":     other_text,
            "msg_snippet":    msg_snippet,
        })
        return jsonify({"status": "ok"})
    except Exception as e:
        print(f"Error saving report: {e}")
        return jsonify({"error": str(e)}), 500


@bp.route("/clear", methods=["POST"])
def clear_memory():
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"})
    session.pop("session_id",       None)
    session.pop("current_conv_id",  None)
    session.pop("created_at",       None)
    return jsonify({"status": "success", "message": "New session started"})


@bp.route("/conversations", methods=["GET"])
def conversations():
    if not session.get("user") or _is_guest():
        return jsonify([])
    return jsonify(list_conversations(session.get("uid")))


@bp.route("/conversation/<conv_id>", methods=["GET"])
def conversation(conv_id):
    if not session.get("user") or _is_guest():
        return jsonify({"error": "Not authenticated"}), 401
    conv = get_conversation(session.get("uid"), conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    return jsonify(conv)


@bp.route("/conversation/<conv_id>/restore", methods=["POST"])
def restore_conversation(conv_id):
    if not session.get("user") or _is_guest():
        return jsonify({"error": "Not authenticated"}), 401
    conv = get_conversation(session.get("uid"), conv_id)
    if not conv or "messages" not in conv:
        return jsonify({"error": "Conversation not found"}), 404

    session_id = get_session_id()
    app_graph.update_state(
        config={"configurable": {"thread_id": session_id}},
        values={"chat_history": conv["messages"]},
    )
    session["current_conv_id"] = conv_id
    session["created_at"]      = conv.get(
        "created_at",
        datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    return jsonify({"status": "success", "message": "Conversation restored"})


@bp.route("/conversation/<conv_id>/delete", methods=["DELETE"])
def delete_conversation(conv_id):
    if not session.get("user") or _is_guest():
        return jsonify({"error": "Not authenticated"}), 401
    delete_conversation_from_db(session.get("uid"), conv_id)
    return jsonify({"status": "success", "message": "Conversation deleted"})


@bp.route("/index")
def index():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    return redirect(url_for("admin.dashboard") if is_admin() else url_for("chat.chat_page"))