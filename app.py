from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from dotenv import load_dotenv
from src.helper import get_local_embeddings
from src.prompt import *
from langchain_pinecone import PineconeVectorStore
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict
import os
import uuid
import datetime
import tempfile

# -------- AWS --------
import boto3
from botocore.exceptions import ClientError
from jose import jwt
from boto3.dynamodb.conditions import Key

from store_index import append_file_to_index

# ====== Flask App ======
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "Clone_rev1")

# ====== Load .env ======
BASE_DIR = os.path.dirname(__file__)
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=env_path)

# ====== AWS Config ======
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

cognito_client = boto3.client("cognito-idp", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
files_table = dynamodb.Table("Files")
conversations_table = dynamodb.Table("Conversations")

# ====== API KEYS ======
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
os.environ["PINECONE_API_KEY"] = PINECONE_API_KEY or ""
os.environ["OPENROUTER_API_KEY"] = OPENROUTER_API_KEY or ""

# ====== Vector Store ======
embeddings = get_local_embeddings()
index_name = "rag-database3"
docsearch = PineconeVectorStore.from_existing_index(index_name=index_name, embedding=embeddings)
retriever = docsearch.as_retriever(search_type="similarity", search_kwargs={"k": 7})

# ====== LLM ======
chatModel = ChatOpenAI(
    model="openrouter/sonoma-dusk-alpha",
    openai_api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    temperature=0.7,
    max_tokens=2048
)

# 🔹 Summarizer LLM (lighter/faster)
summarizer = ChatOpenAI(
    model="openrouter/sonoma-dusk-alpha",
    openai_api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    temperature=0.3,
    max_tokens=512
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
contextualized_prompt.input_variables.append("retrieved_docs")

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

# 🔹 Summarization Helper
def summarize_history(history: List[Dict[str, str]]) -> str:
    if not history:
        return ""
    transcript = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    summary_prompt = [
        {"role": "system", "content": "Summarize the following conversation. Keep it concise but preserve key facts, questions, and answers."},
        {"role": "user", "content": transcript}
    ]
    response = summarizer.invoke(summary_prompt)
    return response.content

# ====== Graph ======
graph = StateGraph(ChatState)

def call_llm(state: ChatState):
    # 1. Retrieve documents
    docs = retriever.invoke(state["input"])
    retrieved_docs_context = docs_to_context(docs)

    # 2. Get the user-facing history and create a temporary copy for the LLM
    history_for_state = state.get("chat_history", [])
    history_for_llm = list(history_for_state)

    # 3. Summarize the temporary copy if the conversation is long
    X = 20
    if len(history_for_llm) > X:
        summary = summarize_history(history_for_llm[:-5])
        history_for_llm = [{"role": "system", "content": f"This is a summary of the preceding conversation: {summary}"}] + history_for_llm[-5:]

    # 4. PROMPT BUILDING LOGIC
    final_system_prompt = system_prompt.format(
        retrieved_docs=retrieved_docs_context,
        chat_history="" 
    )
    final_messages_for_llm = [("system", final_system_prompt)]
    for msg in history_for_llm:
        final_messages_for_llm.append((msg['role'], msg['content']))
    final_messages_for_llm.append(("human", state["input"]))

    # 5. Invoke the model
    response = chatModel.invoke(final_messages_for_llm)

    # 6. Update the state
    updated_history = history_for_state + [
        {"role": "user", "content": state["input"]},
        {"role": "assistant", "content": response.content}
    ]
    return {"answer": response.content, "chat_history": updated_history}

graph.add_node("llm", call_llm)
graph.set_entry_point("llm")
graph.add_edge("llm", END)
app_graph = graph.compile(checkpointer=checkpointer)

# ====== Cognito Helpers ======
def get_user_role_from_claims(id_token):
    decoded = jwt.get_unverified_claims(id_token)
    return decoded.get("custom:role", "user")

def is_admin():
    return session.get("role") == "admin"

# ====== DynamoDB Helpers ======
def save_file_metadata(filename, uid):
    files_table.put_item(Item={
        "filename": filename,
        "uploaded_by": uid,
        "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })

def upsert_conversation(uid, conv_id, history, created_at):
    if not history:
        return
    title = next((m["content"][:40] for m in history if m["role"] == "user"), "Untitled Chat")
    item = {
        "conv_id": conv_id,
        "uid": uid,
        "messages": history,
        "title": title,
        "created_at": created_at,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    conversations_table.put_item(Item=item)

def list_conversations(uid):
    resp = conversations_table.query(
        IndexName="uid-index",
        KeyConditionExpression=Key("uid").eq(uid),
        ScanIndexForward=False
    )
    return resp.get("Items", [])

def get_conversation(uid, conv_id):
    resp = conversations_table.get_item(Key={"conv_id": conv_id, "uid": uid})
    return resp.get("Item")

# Centralized dictionary for user-friendly Cognito error messages
COGNITO_ERROR_MESSAGES = {
    "UsernameExistsException": "This username or email is already registered. Please try logging in.",
    "InvalidPasswordException": "Your password is not strong enough. It must be at least 8 characters long and include an uppercase letter, a lowercase letter, a number, and a special character (e.g., !@#$%).",
    "InvalidParameterException": "Please provide a valid email address and username. Usernames cannot be email addresses.",
    "NotAuthorizedException": "Incorrect username or password. Please check your credentials and try again.",
    "UserNotFoundException": "Incorrect username or password. Please check your credentials and try again.",
    "UserNotConfirmedException": "Your account is not confirmed yet. Please check your email for a confirmation link.",
    "TooManyRequestsException": "You've made too many requests. Please wait a moment and try again.",
    "InternalErrorException": "An internal server error occurred. Please try again later."
}


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
        return jsonify({"success": False, "message": "All fields (username, email, and password) are required."})
    
    if "@" in username:
        return jsonify({"success": False, "message": "Username cannot be an email address."})

    if ' ' in username:
        return jsonify({"success": False, "message": "Username cannot contain spaces."})

    try:
        resp = cognito_client.sign_up(
            ClientId=COGNITO_CLIENT_ID,
            Username=username,
            Password=password,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "name", "Value": username},
                {"Name": "custom:role", "Value": "user"}
            ]
        )
        
        try:
            cognito_client.admin_confirm_sign_up(
                UserPoolId=COGNITO_USER_POOL_ID,
                Username=username
            )
        except ClientError:
            pass 
        
        try:
            cognito_client.admin_update_user_attributes(
                UserPoolId=COGNITO_USER_POOL_ID,
                Username=username,
                UserAttributes=[
                    {'Name': 'email_verified', 'Value': 'true'}
                ]
            )
        except ClientError as e:
            print(f"Could not verify email for {username}: {e}")
            pass
        
        session.update({
            "user": email,
            "uid": resp.get("UserSub"),
            "username": username,
            "role": "user"
        })
        return jsonify({"success": True, "redirect": url_for("chat_page")})

    except ClientError as e:
        err_code = e.response.get("Error", {}).get("Code")
        friendly_message = COGNITO_ERROR_MESSAGES.get(
            err_code, 
            "An unexpected error occurred during signup. Please try again."
        )
        print(f"Cognito Signup Error: {err_code} - {e}")
        return jsonify({"success": False, "message": friendly_message})

@app.route("/login", methods=["POST"])
def login():
    identifier = request.form.get("email")
    password = request.form.get("password")
    
    if not identifier or not password:
        return jsonify({"success": False, "message": "Both identifier and password are required."})

    try:
        resp = cognito_client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": identifier, "PASSWORD": password}
        )
        
        auth_result = resp["AuthenticationResult"]
        id_token = auth_result["IdToken"]
        uid = jwt.get_unverified_claims(id_token)["sub"]
        username = jwt.get_unverified_claims(id_token).get("name", identifier.split("@")[0])
        role = get_user_role_from_claims(id_token)
        session.update({
            "user": identifier,
            "uid": uid,
            "id_token": id_token,
            "username": username,
            "role": role
        })
        
        redirect_url = url_for("dashboard") if role == "admin" else url_for("chat_page")
        return jsonify({"success": True, "redirect": redirect_url})

    except ClientError as e:
        err_code = e.response.get("Error", {}).get("Code")
        friendly_message = COGNITO_ERROR_MESSAGES.get(
            err_code, 
            "An authentication error occurred. Please try again."
        )
        print(f"Cognito Login Error: {err_code} - {e}")
        return jsonify({"success": False, "message": friendly_message})

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
    return render_template("chat.html", user=session["user"])

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
            append_file_to_index(temp_path, index_name=index_name, real_name=file.filename, embeddings=embeddings)
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
    
    resp = files_table.scan()
    items = resp.get("Items", [])
    
    formatted_files = [
        {**item, 'name': item.pop('filename')} 
        for item in items if 'filename' in item
    ]
    
    return jsonify({"success": True, "files": formatted_files})

@app.route("/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    if not session.get("user") or not is_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    try:
        from pinecone import Pinecone
        pc = Pinecone(api_key=PINECONE_API_KEY)
        index = pc.Index(index_name)
        index.delete(filter={"source": {"$eq": filename}})
        files_table.delete_item(Key={"filename": filename})
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

        # Manually sync state from DB before every message
        conv_id = session.get("current_conv_id")
        if conv_id:
            conversation = get_conversation(session.get("uid"), conv_id)
            if conversation and "messages" in conversation:
                app_graph.update_state(
                    config,
                    values={"chat_history": conversation["messages"]}
                )

        result = app_graph.invoke({"input": msg}, config=config)
        answer = result.get("answer", "Sorry, I encountered an issue.")
        updated_history = result.get("chat_history", [])
        
        new_conversation_created = False
        if not conv_id:
            conv_id = str(uuid.uuid4())
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            session["current_conv_id"] = conv_id
            session["created_at"] = created_at
            new_conversation_created = True
        else:
            created_at = session.get("created_at")

        if updated_history:
            upsert_conversation(session.get("uid"), conv_id, updated_history, created_at)

        response_data = {
            "answer": answer,
            "chat_history": updated_history
        }
        if new_conversation_created:
            response_data["new_conversation_created"] = True
            response_data["conv_id"] = conv_id
        
        return jsonify(response_data)

    except Exception as e:
        print(f"Error in /get endpoint: {e}")
        return jsonify({"answer": f"Sorry, an error occurred."}), 500

# ⭐ MODIFIED SECTION START
@app.route("/clear", methods=["POST"])
def clear_memory():
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"})
    try:
        # Instead of clearing memory, we generate a new session ID for a clean slate.
        # This is more robust in multi-worker environments.
        session.pop("session_id", None)
        session.pop("current_conv_id", None)
        session.pop("created_at", None)

        return jsonify({"status": "success", "message": "New session started"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
# ⭐ MODIFIED SECTION END

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
    conversations_table.delete_item(Key={"conv_id": conv_id, "uid": session.get("uid")})
    return jsonify({"status": "success", "message": "Conversation deleted"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)