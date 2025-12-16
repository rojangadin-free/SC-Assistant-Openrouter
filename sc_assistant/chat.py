from flask import (
    Blueprint, render_template, jsonify, request, session, redirect, url_for
)
import datetime
import uuid
from PIL import Image
from rag.chain import app_graph
from aws.dynamodb import (
    upsert_conversation, list_conversations,
    get_conversation, delete_conversation_from_db
)
from .utils import get_session_id, is_admin
from src.helper import encode_image

bp = Blueprint('chat', __name__, url_prefix='/chat')

@bp.route("/")
def chat_page():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    start_new = session.pop('start_new_chat', False)
    
    user_obj = {
        "email": session.get("user"),
        "username": session.get("username", session.get("user", "").split("@")[0])
    }
    
    return render_template("chat.html", user=user_obj, start_new=str(start_new).lower())

@bp.route("/get", methods=["POST"])
def chat():
    if not session.get("user"):
        return jsonify({"error": "Please log in to use the chatbot."}), 401
    try:
        # Retrieve text
        msg = request.form.get("msg", "")
        
        # Retrieve Image
        image_data = None
        if 'image' in request.files and request.files['image'].filename != '':
            image_file = request.files['image']
            try:
                img = Image.open(image_file)
                image_data = encode_image(img)
            except Exception as img_err:
                print(f"Error processing upload: {img_err}")

        session_id = get_session_id()
        config = {"configurable": {"thread_id": session_id}}

        # Fetch existing conversation from DB (for Context & Persistence)
        conv_id = session.get("current_conv_id")
        current_db_history = []
        
        if conv_id:
            conversation = get_conversation(session.get("uid"), conv_id)
            if conversation and "messages" in conversation:
                current_db_history = conversation["messages"]
                # Load DB history into Graph State so LLM has context
                app_graph.update_state(config, values={"chat_history": current_db_history})

        # Construct input
        # FIX: Explicitly set image_data to None if not provided to clear it from Graph state
        input_payload = {
            "input": msg,
            "image_data": image_data if image_data else None
        }

        # Run Graph
        result = app_graph.invoke(input_payload, config=config)
        answer = result.get("answer", "Sorry, I encountered an issue.")
        
        # NOTE: We ignore result.get("chat_history") for saving because it might be 
        # summarized/truncated by the chain logic. We want to save FULL history for UI.

        new_conversation_created = False
        title = None
        if not conv_id:
            conv_id = str(uuid.uuid4())
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            session["current_conv_id"] = conv_id
            session["created_at"] = created_at
            new_conversation_created = True
            title = msg[:40] if msg else "Image Query"
        else:
            created_at = session.get("created_at")

        # FIX: Manually append new messages to the FULL existing history
        new_messages = [
            {"role": "user", "content": msg + (" [Image Uploaded]" if image_data else "")},
            {"role": "assistant", "content": answer}
        ]
        
        # Combine old DB history with new messages
        full_history_to_save = current_db_history + new_messages
        
        # Save to DynamoDB
        upsert_conversation(session.get("uid"), conv_id, full_history_to_save, created_at)

        response_data = {
            "answer": answer, "conv_id": conv_id,
            "new_conversation_created": new_conversation_created,
            "new_conv_title": title
        }
        return jsonify(response_data)
        
    except Exception as e:
        print(f"Error in /get endpoint: {e}")
        return jsonify({"answer": f"Sorry, an error occurred: {str(e)}"}), 500

@bp.route("/clear", methods=["POST"])
def clear_memory():
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"})
    session.pop("session_id", None)
    session.pop("current_conv_id", None)
    session.pop("created_at", None)
    return jsonify({"status": "success", "message": "New session started"})

@bp.route("/conversations", methods=["GET"])
def conversations():
    if not session.get("user"):
        return jsonify([])
    return jsonify(list_conversations(session.get("uid")))

@bp.route("/conversation/<conv_id>", methods=["GET"])
def conversation(conv_id):
    if not session.get("user"):
        return jsonify({"error": "Not authenticated"}), 401
    conv = get_conversation(session.get("uid"), conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    return jsonify(conv)

@bp.route("/conversation/<conv_id>/restore", methods=["POST"])
def restore_conversation(conv_id):
    if not session.get("user"):
        return jsonify({"error": "Not authenticated"}), 401
    conv = get_conversation(session.get("uid"), conv_id)
    if not conv or "messages" not in conv:
        return jsonify({"error": "Conversation not found"}), 404

    session_id = get_session_id()
    app_graph.update_state(
        config={"configurable": {"thread_id": session_id}},
        values={"chat_history": conv["messages"]}
    )
    session["current_conv_id"] = conv_id
    session["created_at"] = conv.get("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
    return jsonify({"status": "success", "message": "Conversation restored"})

@bp.route("/conversation/<conv_id>/delete", methods=["DELETE"])
def delete_conversation(conv_id):
    if not session.get("user"):
        return jsonify({"error": "Not authenticated"}), 401
    delete_conversation_from_db(session.get("uid"), conv_id)
    return jsonify({"status": "success", "message": "Conversation deleted"})

@bp.route("/index")
def index():
    if not session.get("user"):
        return redirect(url_for("auth.auth_page"))
    return redirect(url_for("admin.dashboard") if is_admin() else url_for("chat.chat_page"))