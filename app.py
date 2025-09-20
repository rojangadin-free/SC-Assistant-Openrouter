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
import re
from flashrank import Ranker, RerankRequest

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
    model="x-ai/grok-4-fast:free",
    openai_api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    temperature=0.7,
    max_tokens=2048
)

# 🔹 Summarizer / utility LLM (low-temp, used for rewrites & summarization)
summarizer = ChatOpenAI(
    model="openai/gpt-oss-20b:free",
    openai_api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    temperature=0.1,
    max_tokens=512
)

# ====== Query Rewriter / Gibberish Detection Config ======
# Heuristic thresholds (tweakable)
MIN_WORDS_TO_REWRITE = 4         # only consider rewriting if more than this many words
GIBBERISH_MIN_VOWEL_RATIO = 0.08 # if vowel ratio is less than this -> gibberish
GIBBERISH_MAX_RUN_LENGTH = 12    # long continuous character runs suggest gibberish

CASUAL_PHRASES = [
    "hello", "hi", "hey", "who are you", "what's up", "how are you",
    "thanks", "thank you", "bye", "goodbye"
]

REWRITE_PROMPT = """
Rewrite the user's input to a clear question that preserves the user's intent for a retrieval system.
Return only the rewritten query (no explanation). If the query is already clear and short, return it unchanged.

User input:
\"\"\"{query}\"\"\"
"""

# Initialize FlashRank (this downloads the reranker model once)
flash_reranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2")
# Other good ones: "ms-marco-MiniLM-L-6-v2", "ms-marco-distilbert-base-tas-b"

def rerank_results_flashrank(query: str, docs):
    """Use FlashRank (cross-encoder) to rerank retrieved documents."""
    if not docs:
        return []

    # Build request with docs
    candidates = [{"id": str(i), "text": d.page_content} for i, d in enumerate(docs)]
    request = RerankRequest(query=query, passages=candidates)

    # Run FlashRank
    results = flash_reranker.rerank(request)

    # Map back to original docs by ranked order
    ranked_docs = []
    for r in results:
        doc_id = int(r["id"])
        ranked_docs.append(docs[doc_id])

    return ranked_docs

def looks_like_gibberish(query: str) -> bool:
    """Heuristic checks for gibberish / random character strings."""
    s = query.strip()
    if not s:
        return True

    # If contains many non-alphanumeric characters (excluding punctuation), consider gibberish
    non_alnum = len([c for c in s if not c.isalnum() and not c.isspace() and c not in ".,?!'\"-:/"])
    if non_alnum / max(1, len(s)) > 0.25:
        return True

    # Vowel ratio check
    vowels = len(re.findall(r"[aeiouAEIOU]", s))
    vowel_ratio = vowels / max(1, len(re.findall(r"[A-Za-z]", s)))
    if vowel_ratio > 0 and vowel_ratio < GIBBERISH_MIN_VOWEL_RATIO:
        return True

    # Long runs of consonants or same char
    if re.search(r"(.)\1{6,}", s):  # same char repeats >6 times
        return True
    if re.search(r"[bcdfghjklmnpqrstvwxyz]{6,}", s, re.IGNORECASE):  # long consonant run
        return True

    # If single long alpha token (> GIBBERISH_MAX_RUN_LENGTH) with no spaces, likely gibberish
    tokens = s.split()
    if len(tokens) == 1 and len(tokens[0]) > GIBBERISH_MAX_RUN_LENGTH and tokens[0].isalpha():
        return True

    return False

def should_rewrite_query(query: str) -> bool:
    """Decide whether to send the query to the rewriter."""
    if not query or not query.strip():
        return False

    q = query.strip()
    q_lower = q.lower()

    # Short queries / greetings
    if len(q.split()) <= 3:
        # still allow certain short but meaningful queries to be rewritten? No — keep simple.
        return False

    # small talk / casual phrases -> skip
    for phrase in CASUAL_PHRASES:
        if phrase in q_lower:
            return False

    # if appears to be a URL or email or pure numbers -> skip
    if re.match(r"^(https?://|www\.)", q_lower) or re.match(r"^[\w\.-]+@[\w\.-]+$", q_lower):
        return False
    if q.isnumeric():
        return False

    # gibberish -> skip
    if looks_like_gibberish(q):
        return False

    # otherwise rewrite
    return True

def rewrite_query_llm(query: str) -> str:
    """Call the utility LLM to rewrite the query. Returns the rewritten query or original on failure."""
    prompt = REWRITE_PROMPT.format(query=query)
    try:
        resp = summarizer.invoke([("system", "You are a helpful rewrite assistant."), ("user", prompt)])
        rewritten = resp.content.strip()
        # quick clean: remove surrounding quotes and truncate
        rewritten = rewritten.strip(' "\'')
        # if rewrite is empty or looks like gibberish, return original
        if not rewritten or looks_like_gibberish(rewritten):
            print("Rewriter produced empty or gibberish output — falling back to original query.")
            return query
        return rewritten
    except Exception as e:
        print(f"Query rewriter failed: {e}. Using original query.")
        return query

def process_query(query: str) -> (str, bool):
    """
    Process the incoming query:
      - decide whether to rewrite
      - perform rewrite if needed
    Returns (final_query, was_rewritten_flag)
    """
    if not should_rewrite_query(query):
        # skip rewriting
        print(f"[rewriter] Skipping rewrite for: {query!r}")
        return query, False

    # try rewriting
    rewritten = rewrite_query_llm(query)
    if rewritten and rewritten.strip().lower() != query.strip().lower():
        print(f"[rewriter] Rewrote query: {query!r} -> {rewritten!r}")
        return rewritten, True
    else:
        print(f"[rewriter] Rewriter returned identical query, using original.")
        return query, False

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

# 🔹 Summarization Helper (uses summarizer LLM)
def summarize_history(history: List[Dict[str, str]]) -> str:
    if not history:
        return ""
    transcript = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    summary_prompt = [
        {"role": "system", "content": "Summarize the following conversation. Keep it concise but preserve key facts, questions, and answers."},
        {"role": "user", "content": transcript}
    ]
    try:
        response = summarizer.invoke(summary_prompt)
        return response.content
    except Exception as e:
        print(f"Summarization failed: {e}")
        return ""

# ====== Graph ======
graph = StateGraph(ChatState)

def call_llm(state: ChatState):
    # 0. Original user input
    user_input = state["input"]

    # 1. Rewrite query if useful
    final_query, was_rewritten = process_query(user_input)

    # 2. Retrieve docs with original + rewritten query
    docs = []
    try:
        docs.extend(retriever.invoke(user_input))
    except Exception as e:
        print(f"Retriever failed with original query '{user_input}': {e}")

    if was_rewritten and final_query.strip().lower() != user_input.strip().lower():
        try:
            docs.extend(retriever.invoke(final_query))
        except Exception as e:
            print(f"Retriever failed with rewritten query '{final_query}': {e}")

    # Deduplicate docs
    seen = set()
    unique_docs = []
    for d in docs:
        if d.page_content not in seen:
            unique_docs.append(d)
            seen.add(d.page_content)

   # 3. Rerank combined results with FlashRank
    if unique_docs:
        rerank_query = final_query if was_rewritten else user_input
        ranked_docs = rerank_results_flashrank(rerank_query, unique_docs)
    else:
        ranked_docs = []


    retrieved_docs_context = docs_to_context(ranked_docs)

    # 4. Conversation history
    history_for_state = state.get("chat_history", [])
    history_for_llm = list(history_for_state)

    # 5. Summarize history if long
    X = 20
    if len(history_for_llm) > X:
        summary = summarize_history(history_for_llm[:-5])
        if summary:
            history_for_llm = [{"role": "system", "content": f"Summary of earlier conversation: {summary}"}] + history_for_llm[-5:]
        else:
            history_for_llm = history_for_llm[-10:]

    # 6. Prompt
    rewrite_note = ""
    if was_rewritten and final_query.strip().lower() != user_input.strip().lower():
        rewrite_note = f"\n\n(Note: user originally asked: \"{user_input}\", rewritten as: \"{final_query}\")"

    final_system_prompt = system_prompt.format(
        retrieved_docs=retrieved_docs_context + rewrite_note,
        chat_history=""
    )

    final_messages_for_llm = [("system", final_system_prompt)]
    for msg in history_for_llm:
        final_messages_for_llm.append((msg['role'], msg['content']))
    final_messages_for_llm.append(("human", user_input))

    # 7. Model call
    try:
        response = chatModel.invoke(final_messages_for_llm)
    except Exception as e:
        print(f"Main LLM call failed: {e}")
        try:
            fallback_resp = summarizer.invoke([("system", "You are a helpful assistant."), ("user", user_input)])
            response = fallback_resp
        except Exception as e2:
            print(f"Fallback LLM also failed: {e2}")
            class _Resp: pass
            response = _Resp()
            response.content = "Sorry, I couldn't process that right now."

    # 8. Update state
    updated_history = history_for_state + [
        {"role": "user", "content": user_input},
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
    "InternalErrorException": "An internal server error occurred. Please try again later.",
    # New codes for password reset
    "CodeMismatchException": "The verification code is incorrect. Please check the code and try again.",
    "ExpiredCodeException": "The verification code has expired. Please request a new one.",
    "LimitExceededException": "You have exceeded the limit for password reset attempts. Please try again later."
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
        
        session['start_new_chat'] = True

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

@app.route("/forgot-password", methods=["POST"])
def forgot_password():
    email = request.form.get("email")
    if not email:
        return jsonify({"success": False, "message": "An email address is required."})

    try:
        cognito_client.forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            Username=email
        )
        return jsonify({
            "success": True,
            "message": "If an account with that email exists, you will receive a code to reset your password."
        })

    except ClientError as e:
        err_code = e.response.get("Error", {}).get("Code")
        print(f"Cognito Forgot Password Error: {err_code} - {e}")

        if err_code == "UserNotFoundException":
            return jsonify({
                "success": True,
                "message": "If an account with that email exists, you will receive a code to reset your password."
            })

        friendly_message = COGNITO_ERROR_MESSAGES.get(
            err_code,
            "An unexpected error occurred. Please try again."
        )
        return jsonify({"success": False, "message": friendly_message})

@app.route("/reset-password", methods=["POST"])
def reset_password():
    email = request.form.get("email")
    code = request.form.get("code")
    new_password = request.form.get("password")

    if not all([email, code, new_password]):
        return jsonify({"success": False, "message": "Email, code, and a new password are required."})

    try:
        cognito_client.confirm_forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            Username=email,
            ConfirmationCode=code,
            Password=new_password
        )
        return jsonify({"success": True, "message": "Password has been reset successfully!"})
    except ClientError as e:
        err_code = e.response.get("Error", {}).get("Code")
        friendly_message = COGNITO_ERROR_MESSAGES.get(err_code, "An unexpected error occurred.")
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
        title = None
        if not conv_id:
            conv_id = str(uuid.uuid4())
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            session["current_conv_id"] = conv_id
            session["created_at"] = created_at
            new_conversation_created = True
            title = msg[:40]  # Capture title for new conversations
        else:
            created_at = session.get("created_at")

        if updated_history:
            upsert_conversation(session.get("uid"), conv_id, updated_history, created_at)

        # ⭐ MODIFIED RESPONSE DATA: No longer sending full chat_history
        response_data = {
            "answer": answer,
            "conv_id": conv_id,
            "new_conversation_created": new_conversation_created,
            "new_conv_title": title  # Send title back if new conv was created
        }

        return jsonify(response_data)

    except Exception as e:
        print(f"Error in /get endpoint: {e}")
        return jsonify({"answer": f"Sorry, an error occurred."}), 500

@app.route("/clear", methods=["POST"])
def clear_memory():
    if not session.get("user"):
        return jsonify({"status": "error", "message": "Not authenticated"})
    try:
        session.pop("session_id", None)
        session.pop("current_conv_id", None)
        session.pop("created_at", None)

        return jsonify({"status": "success", "message": "New session started"})
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