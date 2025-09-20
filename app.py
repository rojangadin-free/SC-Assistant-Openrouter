from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from jose import jwt
import os
import uuid
import datetime
import tempfile

from config import FLASK_SECRET_KEY, INDEX_NAME, PINECONE_API_KEY
from aws.cognito import (
    sign_up_user, login_user, forgot_password, reset_password,
    get_user_role_from_claims
)
from aws.dynamodb import (
    save_file_metadata, upsert_conversation, list_conversations,
    get_conversation, delete_conversation_from_db, delete_file_from_db,
    files_table
)
from rag.chain import app_graph, embeddings
from store_index import append_file_to_index

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

def is_admin():
    """Checks if the current user is an admin."""
    return session.get("role") == "admin"

def get_session_id():
    """Gets or creates a unique session ID."""
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return session["session_id"]

# ====== Auth Routes ======
@app.route("/auth")
def auth_page():
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("auth.html")

@app.route("/signup", methods=["POST"])
def signup():
    email = request.form.get("email")
    password = request.form.get("password")
    username = request.form.get("username")

    if not all([email, password, username]):
        return jsonify({"success": False, "message": "All fields are required."})

    result = sign_up_user(username, email, password)
    if result["success"]:
        session.update({
            "user": email, "uid": result.get("user_sub"),
            "username": username, "role": "user"
        })
        return jsonify({"success": True, "redirect": url_for("chat_page")})
    return jsonify(result)

@app.route("/login", methods=["POST"])
def login():
    identifier = request.form.get("email")
    password = request.form.get("password")

    if not all([identifier, password]):
        return jsonify({"success": False, "message": "Both identifier and password are required."})

    result = login_user(identifier, password)
    if result["success"]:
        auth_result = result["auth_result"]
        id_token = auth_result["IdToken"]
        claims = jwt.get_unverified_claims(id_token)
        session.update({
            "user": identifier, "uid": claims["sub"], "id_token": id_token,
            "username": claims.get("name", identifier.split("@")[0]),
            "role": get_user_role_from_claims(id_token)
        })
        session['start_new_chat'] = True
        redirect_url = url_for("dashboard") if is_admin() else url_for("chat_page")
        return jsonify({"success": True, "redirect": redirect_url})
    return jsonify(result)

@app.route("/forgot-password", methods=["POST"])
def handle_forgot_password():
    email = request.form.get("email")
    if not email:
        return jsonify({"success": False, "message": "An email address is required."})
    return jsonify(forgot_password(email))

@app.route("/reset-password", methods=["POST"])
def handle_reset_password():
    email = request.form.get("email")
    code = request.form.get("code")
    new_password = request.form.get("password")

    if not all([email, code, new_password]):
        return jsonify({"success": False, "message": "Email, code, and new password are required."})
    return jsonify(reset_password(email, code, new_password))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth_page"))

# ====== Role-based Routes ======
@app.route("/dashboard")
def dashboard():
    if not session.get("user") or not is_admin():
        return redirect(url_for("chat_page"))
    return render_template("dashboard.html", user=session["user"])

@app.route("/chat")
def chat_page():
    if not session.get("user"):
        return redirect(url_for("auth_page"))
    start_new = session.pop('start_new_chat', False)
    return render_template("chat.html", user=session["user"], start_new_chat=str(start_new).lower())

@app.route("/")
def index():
    if not session.get("user"):
        return redirect(url_for("auth_page"))
    return redirect(url_for("dashboard") if is_admin() else url_for("chat_page"))

# ====== File Upload / Delete ======
@app.route("/upload", methods=["POST"])
def upload_file():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    if "files[]" not in request.files:
        return jsonify({"success": False, "message": "No file part"}), 400

    files = request.files.getlist("files[]")
    processed_files = []
    for file in files:
        if not file.filename:
            continue
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            file.save(tmp.name)
            temp_path = tmp.name
        try:
            append_file_to_index(temp_path, index_name=INDEX_NAME, real_name=file.filename, embeddings=embeddings)
            save_file_metadata(file.filename, session.get("uid"))
            processed_files.append(file.filename)
        finally:
            os.remove(temp_path)

    if processed_files:
        return jsonify({"success": True, "files": processed_files, "message": "Files uploaded & indexed successfully"})
    return jsonify({"success": False, "message": "No files uploaded"}), 400

@app.route("/files", methods=["GET"])
def list_files():
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    items = files_table.scan().get("Items", [])
    formatted_files = [{**item, 'name': item.pop('filename')} for item in items if 'filename' in item]
    return jsonify({"success": True, "files": formatted_files})

@app.route("/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        from pinecone import Pinecone
        pc = Pinecone(api_key=PINECONE_API_KEY)
        index = pc.Index(INDEX_NAME)
        index.delete(filter={"source": {"$eq": filename}})
        delete_file_from_db(filename)
        return jsonify({"success": True, "message": f"Deleted {filename} and its vectors"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# ====== Chat API ======
@app.route("/get", methods=["POST"])
def chat():
    if not session.get("user"):
        return jsonify({"error": "Please log in to use the chatbot."}), 401
    try:
        msg = request.form["msg"]
        session_id = get_session_id()
        config = {"configurable": {"thread_id": session_id}}

        conv_id = session.get("current_conv_id")
        if conv_id:
            conversation = get_conversation(session.get("uid"), conv_id)
            if conversation and "messages" in conversation:
                app_graph.update_state(config, values={"chat_history": conversation["messages"]})

        result = app_graph.invoke({"input": msg}, config=config)
        answer = result.get("answer", "Sorry, I encountered an issue.")
        updated_history = result.get("chat_history", [])

        new_conversation_created = False
        title = None
        if not conv_id:
            conv_id = str(uuid.uuid4())
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            session["current_conv_id"] = conv_id
            session["created_at"] = created_at
            new_conversation_created = True
            title = msg[:40]
        else:
            created_at = session.get("created_at")

        if updated_history:
            upsert_conversation(session.get("uid"), conv_id, updated_history, created_at)

        response_data = {
            "answer": answer, "conv_id": conv_id,
            "new_conversation_created": new_conversation_created,
            "new_conv_title": title
        }
        return jsonify(response_data)
    except Exception as e:
        print(f"Error in /get endpoint: {e}")
        return jsonify({"answer": f"Sorry, an error occurred."}), 500

@app.route("/clear", methods=["POST"])
def clear_memory():
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"})
    session.pop("session_id", None)
    session.pop("current_conv_id", None)
    session.pop("created_at", None)
    return jsonify({"status": "success", "message": "New session started"})

@app.route("/conversations", methods=["GET"])
def conversations():
    if not session.get("user"):
        return jsonify([])
    return jsonify(list_conversations(session.get("uid")))

@app.route("/conversation/<conv_id>", methods=["GET"])
def conversation(conv_id):
    if not session.get("user"):
        return jsonify({"error": "Not authenticated"}), 401
    conv = get_conversation(session.get("uid"), conv_id)
    if not conv:
        return jsonify({"error": "Not found"}), 404
    return jsonify(conv)

@app.route("/conversation/<conv_id>/restore", methods=["POST"])
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

@app.route("/conversation/<conv_id>/delete", methods=["DELETE"])
def delete_conversation(conv_id):
    if not session.get("user"):
        return jsonify({"error": "Not authenticated"}), 401
    delete_conversation_from_db(session.get("uid"), conv_id)
    return jsonify({"status": "success", "message": "Conversation deleted"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)