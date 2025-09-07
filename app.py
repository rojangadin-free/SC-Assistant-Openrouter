from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from dotenv import load_dotenv
from src.helper import get_local_embeddings
from src.prompt import * # expects a `system_prompt` variable
from langchain_pinecone import PineconeVectorStore
from langchain_openai import ChatOpenAI   # ✅ Use ChatOpenAI with base_url
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict
import os
import uuid
import requests
import datetime
import concurrent.futures
import tempfile

# -------- Firebase Admin --------
import firebase_admin
from firebase_admin import credentials, auth, firestore

# -------- Import store_index --------
from store_index import append_file_to_index   # ✅ use append instead of build

# ====== Flask App ======
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-in-production")

load_dotenv()

# ====== API KEYS ======
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")  # ✅ OpenRouter API Key
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY")
FIREBASE_CRED_PATH = os.environ.get("FIREBASE_CRED_PATH", "firebase_key.json")

os.environ["PINECONE_API_KEY"] = PINECONE_API_KEY or ""
os.environ["OPENROUTER_API_KEY"] = OPENROUTER_API_KEY or ""

# ====== Firebase Init ======
db = None
if not firebase_admin._apps:
    try:
        cred = credentials.Certificate(FIREBASE_CRED_PATH)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
    except Exception as e:
        print("❌ Firebase init failed:", e)

# ====== Vector Store ======
embeddings = get_local_embeddings()
index_name = "rag-database3"
docsearch = PineconeVectorStore.from_existing_index(index_name=index_name, embedding=embeddings)
retriever = docsearch.as_retriever(
    search_type="similarity",
    search_kwargs={"k": 4}
)

# ====== LLM ======
# ✅ Switched to ChatOpenAI with OpenRouter base_url
chatModel = ChatOpenAI(
    model="openrouter/sonoma-dusk-alpha",
    openai_api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    temperature=0.7,
    max_tokens=1024
)

# ====== Chat State ======
class ChatState(TypedDict):
    input: str
    chat_history: List[Dict[str, str]]
    answer: str

checkpointer = InMemorySaver()

contextualized_prompt = ChatPromptTemplate.from_messages([
    ("system", system_prompt),
    ("human", "{input}")
])

def docs_to_context(docs) -> str:
    if not docs:
        return "No relevant context found."
    chunks = []
    for i, d in enumerate(docs, start=1):
        src = ""
        try:
            meta = d.metadata or {}
            src = meta.get("source") or meta.get("url") or meta.get("file_name") or meta.get("id") or meta.get("file_id") or ""
            if src:
                src = f" "
        except Exception:
            pass
        chunks.append(f"[{i}] {d.page_content}{src}")
    return "\n\n".join(chunks)

def get_session_id():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return session["session_id"]

# ====== Graph ======
graph = StateGraph(ChatState)

def call_llm(state: ChatState):
    docs = retriever.invoke(state["input"])
    context = docs_to_context(docs)

    msg_with_context = f"{state['input']}\n\n[Context for reference:]\n{context}"
    history_list = state.get("chat_history", [])
    history_text = "\n".join([f"{m['role']}: {m['content']}" for m in history_list])

    prompt_messages = contextualized_prompt.format_messages(chat_history=history_text, input=msg_with_context)
    response = chatModel.invoke(prompt_messages)

    updated_history = history_list + [
        {"role": "user", "content": state["input"]},
        {"role": "assistant", "content": response.content}
    ]
    return {"answer": response.content, "chat_history": updated_history}

graph.add_node("llm", call_llm)
graph.set_entry_point("llm")
graph.add_edge("llm", END)
app_graph = graph.compile(checkpointer=checkpointer)

# ====== Firestore Helpers ======
def safe_get_user_doc(uid, timeout=5):
    if not db or not uid:
        return None
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(lambda: db.collection("users").document(uid).get())
            return future.result(timeout=timeout)
    except Exception as e:
        print(f"⚠️ Firestore lookup failed for uid={uid}: {e}")
        return None

def safe_set_user_doc(uid, data):
    if not db or not uid:
        return
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.submit(lambda: db.collection("users").document(uid).set(data))
    except Exception as e:
        print("⚠️ Firestore write failed:", e)

def get_user_role(uid):
    doc = safe_get_user_doc(uid)
    if doc and doc.exists:
        return doc.to_dict().get("role", "user")
    return "user"

def is_admin(uid):
    return get_user_role(uid) == "admin"

# ---- Conversation Helpers ----
def save_conversation(uid, history):
    if not db or not uid or not history:
        return
    try:
        title = None
        for m in history:
            if m["role"] == "user":
                title = m["content"][:40]
                break
        conv_ref = db.collection("users").document(uid).collection("conversations").document()
        conv_ref.set({
            "title": title or "Untitled Chat",
            "messages": history,
            "created_at": datetime.datetime.utcnow()
        })
    except Exception as e:
        print("⚠️ Failed to save conversation:", e)

def list_conversations(uid):
    if not db or not uid:
        return []
    try:
        docs = db.collection("users").document(uid).collection("conversations").order_by(
            "created_at", direction=firestore.Query.DESCENDING
        ).stream()
        return [{"id": d.id, **d.to_dict()} for d in docs]
    except Exception as e:
        print("⚠️ Failed to list conversations:", e)
        return []

def get_conversation(uid, conv_id):
    if not db or not uid or not conv_id:
        return None
    try:
        doc = db.collection("users").document(uid).collection("conversations").document(conv_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print("⚠️ Failed to fetch conversation:", e)
        return None

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

    if not email or not password or not username:
        return jsonify({"success": False, "message": "All fields are required."})

    try:
        user = auth.create_user(email=email, password=password)
        safe_set_user_doc(user.uid, {
            "email": user.email,
            "username": username,
            "created_at": datetime.datetime.utcnow(),
            "role": "user",
            "active": True
        })
        session.update({
            "user": email,
            "uid": user.uid,
            "username": username,
            "role": "user"
        })
        return jsonify({"success": True, "redirect": url_for("chat_page")})
    except firebase_admin._auth_utils.EmailAlreadyExistsError:
        return jsonify({"success": False, "message": "Email already registered."})
    except Exception as e:
        print("❌ Signup failed:", e)
        return jsonify({"success": False, "message": "Signup failed. Try again."})

@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email")
    password = request.form.get("password")

    if not email or not password:
        return jsonify({"success": False, "message": "All fields are required."})

    if not FIREBASE_WEB_API_KEY:
        return jsonify({"success": False, "message": "Server misconfigured: missing FIREBASE_WEB_API_KEY."})

    try:
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
        payload = {"email": email, "password": password, "returnSecureToken": True}
        r = requests.post(url, json=payload, timeout=15)

        if r.status_code != 200:
            msg = r.json().get("error", {}).get("message", "Login failed")
            friendly = {
                "EMAIL_NOT_FOUND": "Email not found.",
                "INVALID_PASSWORD": "Invalid password.",
                "USER_DISABLED": "This account has been disabled."
            }.get(msg, msg)
            return jsonify({"success": False, "message": friendly})

        data = r.json()
        uid = data.get("localId")

        role = "user"
        username = email.split("@")[0]
        doc = safe_get_user_doc(uid)
        if doc and doc.exists:
            u = doc.to_dict()
            role = u.get("role", "user")
            username = u.get("username", username)

        session.update({
            "user": email,
            "uid": uid,
            "id_token": data.get("idToken"),
            "username": username,
            "role": role
        })

        redirect_url = url_for("dashboard") if role == "admin" else url_for("chat_page")
        return jsonify({"success": True, "redirect": redirect_url})

    except Exception as e:
        print("❌ Login error:", e)
        return jsonify({"success": False, "message": "Login failed. Try again."})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth_page"))

# ====== Role-based Routes ======
@app.route("/dashboard")
def dashboard():
    if not session.get("user"):
        return redirect(url_for("auth_page"))
    if not is_admin(session.get("uid")):
        return redirect(url_for("chat_page"))
    doc = safe_get_user_doc(session.get("uid"))
    return render_template("dashboard.html", user=session["user"], profile=(doc.to_dict() if doc else {}))

@app.route("/chat")
def chat_page():
    if not session.get("user"):
        return redirect(url_for("auth_page"))
    doc = safe_get_user_doc(session.get("uid"))
    return render_template("chat.html", user=session["user"], profile=(doc.to_dict() if doc else {}))

@app.route("/")
def index():
    if not session.get("user"):
        return redirect(url_for("auth_page"))
    return redirect(url_for("dashboard") if is_admin(session.get("uid")) else url_for("chat_page"))

@app.route("/upload", methods=["POST"])
def upload_file():
    if not session.get("user") or not is_admin(session.get("uid")):
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    if "files[]" not in request.files:
        return jsonify({"success": False, "message": "No file part"}), 400

    files = request.files.getlist("files[]")
    processed_files = []

    for file in files:
        if file.filename == "":
            continue

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            file.save(tmp.name)
            temp_path = tmp.name

        try:
            append_file_to_index(
                temp_path,
                index_name="rag-database3",
                real_name=file.filename,  # ✅ use original filename
                embeddings=embeddings
            )

            db.collection("files").document(file.filename).set({
                "file_name": file.filename,
                "uploaded_by": session.get("uid"),
                "uploaded_at": datetime.datetime.utcnow()
            })
            processed_files.append(file.filename)

        except Exception as e:
            print("❌ Append failed:", e)
            return jsonify({"success": False, "message": f"File {file.filename} indexing failed"}), 500
        finally:
            os.remove(temp_path)

    if processed_files:
        return jsonify({
            "success": True,
            "files": processed_files,
            "message": "Files uploaded & indexed successfully"
        })
    else:
        return jsonify({"success": False, "message": "No files uploaded"}), 400

@app.route("/files", methods=["GET"])
def list_files():
    if not session.get("user") or not is_admin(session.get("uid")):
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        files_data = []
        docs = db.collection("files").stream()
        for d in docs:
            file_info = d.to_dict()
            upload_date = file_info.get("uploaded_at")
            files_data.append({
                "name": file_info["file_name"],
                "size": 0,
                "uploaded_at": upload_date.isoformat() if upload_date else None
            })
        return jsonify({"success": True, "files": files_data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

from pinecone import Pinecone
PINECONE_INDEX_NAME = "rag-database3"

@app.route("/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    if not session.get("user") or not is_admin(session.get("uid")):
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        pc = Pinecone(api_key=PINECONE_API_KEY)
        index = pc.Index(PINECONE_INDEX_NAME)

        index.delete(filter={"source": {"$eq": filename}})
        db.collection("files").document(filename).delete()

        return jsonify({"success": True, "message": f"Deleted {filename} and its vectors"})

    except Exception as e:
        print(f"❌ Error deleting file {filename}: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

# ====== Chat API ======
@app.route("/get", methods=["POST"])
def chat():
    if not session.get("user"):
        return "Please log in to use the chatbot."
    try:
        msg = request.form["msg"]
        result = app_graph.invoke({"input": msg}, config={"configurable": {"thread_id": get_session_id()}})
        return str(result["answer"])
    except Exception as e:
        print(f"❌ Chat error: {e}")
        return f"Sorry, error: {str(e)}"

@app.route("/clear", methods=["POST"])
def clear_memory():
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"})
    try:
        session_id = get_session_id()
        snapshot = app_graph.get_state(config={"configurable": {"thread_id": session_id}})
        history = getattr(snapshot, "values", {}).get("chat_history", [])
        if history:
            save_conversation(session.get("uid"), history)
        app_graph.update_state(config={"configurable": {"thread_id": session_id}}, values={"chat_history": []})
        return jsonify({"status": "success", "message": "Conversation saved & memory cleared"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

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
    try:
        session_id = get_session_id()
        app_graph.update_state(
            config={"configurable": {"thread_id": session_id}},
            values={"chat_history": conv["messages"]}
        )
        return jsonify({"status": "success", "message": "Conversation restored"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/conversation/<conv_id>/delete", methods=["DELETE"])
def delete_conversation(conv_id):
    if not session.get("user"):
        return jsonify({"error": "Not authenticated"}), 401
    try:
        db.collection("users").document(session["uid"]).collection("conversations").document(conv_id).delete()
        return jsonify({"status": "success", "message": "Conversation deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001)