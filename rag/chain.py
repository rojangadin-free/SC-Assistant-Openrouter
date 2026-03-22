import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict, Optional
from pinecone import Pinecone

from langchain_community.retrievers import PineconeHybridSearchRetriever
from langchain.retrievers import EnsembleRetriever
from pinecone_text.sparse import BM25Encoder
from sentence_transformers import CrossEncoder

from src.helper import get_local_embeddings
from src.prompt import system_prompt
from config import INDEX_NAME, CHAT_MODEL_NAME, SUMMARIZER_MODEL_NAME, GOOGLE_API_KEY, PINECONE_API_KEY

# Student records
from aws.students import get_student_by_uid, get_student_by_email, format_student_context

# ====== Setup ======
embeddings = get_local_embeddings()
pinecone = Pinecone(api_key=PINECONE_API_KEY)
index = pinecone.Index(INDEX_NAME)

# --- Retrievers ---
bm25_path = "bm25_values.json"
if os.path.exists(bm25_path):
    bm25 = BM25Encoder().load(bm25_path)
else:
    print("WARNING: bm25_values.json not found. Using default BM25.")
    bm25 = BM25Encoder().default()

sparse_retriever = PineconeHybridSearchRetriever(
    embeddings=embeddings,
    sparse_encoder=bm25,
    index=index,
    top_k=10,
    alpha=0.0,
)

dense_retriever = PineconeHybridSearchRetriever(
    embeddings=embeddings,
    sparse_encoder=bm25,
    index=index,
    top_k=10,
    alpha=1.0,
)

retriever = EnsembleRetriever(
    retrievers=[sparse_retriever, dense_retriever],
    weights=[0.5, 0.5],
)

# Cross-encoder reranker
reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512)
RERANK_TOP_K = 8


def rerank_docs(query: str, docs: list) -> list:
    if not docs:
        return docs
    pairs  = [(query, doc.page_content) for doc in docs]
    scores = reranker.predict(pairs)
    ranked = sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)
    top    = [doc for _, doc in ranked[:RERANK_TOP_K]]
    print(f"  Reranked {len(docs)} → top {len(top)} docs")
    return top


chatModel = ChatGoogleGenerativeAI(
    model=CHAT_MODEL_NAME,
    google_api_key=GOOGLE_API_KEY,
    temperature=0.3,
    max_tokens=None,
    timeout=None,
    max_retries=2,
)

summarizer = ChatGoogleGenerativeAI(
    model=SUMMARIZER_MODEL_NAME,
    google_api_key=GOOGLE_API_KEY,
    temperature=0,
)


# ====== Chat State ======
class ChatState(TypedDict):
    input: str
    image_data: Optional[str]
    chat_history: List[Dict[str, str]]
    answer: str
    # Optional — injected per request, not stored in LangGraph state
    uid: Optional[str]
    user_email: Optional[str]


# Student record is fetched for EVERY logged-in user on every request.
# The LLM decides whether to use it — no brittle keyword matching needed.
# Cost: one DynamoDB GetItem per message (~1ms, negligible).
_STUDENT_CACHE: dict = {}   # in-process cache: uid → record (cleared on restart)


def docs_to_context(docs) -> str:
    """Format retrieved docs with clear citation anchors."""
    context_parts = []
    for i, d in enumerate(docs):
        source = d.metadata.get("source", "Unknown")
        page   = d.metadata.get("page", "?")
        text   = (
            f"[DOCUMENT {i + 1}]\n"
            f"Source: {source} | Page: {page}\n"
            f"Content:\n{d.page_content}"
        )
        context_parts.append(text)
    if not context_parts:
        return "No relevant context found."
    return "\n\n---\n\n".join(context_parts)


def summarize_history(history: List[Dict[str, str]]) -> str:
    if not history:
        return ""
    transcript = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    prompt = [
        {"role": "system", "content": "Summarize the conversation to retain key context."},
        {"role": "user",   "content": transcript},
    ]
    return summarizer.invoke(prompt).content


# ====== LangGraph ======
def create_graph():
    graph = StateGraph(ChatState)

    def call_llm(state: ChatState):
        user_text  = state["input"]
        image_data = state.get("image_data")
        history    = state.get("chat_history", [])
        uid        = state.get("uid")
        user_email = state.get("user_email")

        # ── 1. Image Analysis ──────────────────────────────────
        if image_data:
            vision_msg = HumanMessage(content=[
                {"type": "text",
                 "text": "Transcribe any text in this image and describe the visual layout in detail."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
            ])
            image_description = summarizer.invoke([vision_msg]).content
            print(f"Image Analysis: {image_description}")

        # ── 2. Always fetch student record for logged-in users ────
        # No keyword guessing — the LLM sees the record on every turn
        # and uses it only when the question warrants it.
        student_context = ""
        if uid:
            # Check in-process cache first (avoids DynamoDB on every token)
            if uid not in _STUDENT_CACHE:
                student = get_student_by_uid(uid)
                if not student and user_email:
                    student = get_student_by_email(user_email)
                _STUDENT_CACHE[uid] = student  # None is cached too (no record)
                if student:
                    print(f"  Student record fetched: {student.get('full_name')}")
                else:
                    print("  No student record found for this user.")
            else:
                student = _STUDENT_CACHE[uid]

            if student:
                student_context = format_student_context(student)

        # ── 3. Retrieval (Ensemble RRF) ────────────────────────
        print(f"\n=== STEP 1: Retrieval (Query: '{user_text}') ===")
        initial_docs = retriever.invoke(user_text)
        print(f"  RRF returned {len(initial_docs)} candidates")

        # ── 4. Reranking ───────────────────────────────────────
        print(f"\n=== STEP 2: Reranking to top {RERANK_TOP_K} ===")
        reranked_docs = rerank_docs(user_text, initial_docs)
        for i, doc in enumerate(reranked_docs):
            src     = doc.metadata.get("source", "Unknown")
            pg      = doc.metadata.get("page", "?")
            snippet = doc.page_content.replace("\n", " ")[:80]
            print(f"  [{i+1}] {src} (Pg {pg}): {snippet}...")

        context_str = docs_to_context(reranked_docs)

        # ── 5. History management ──────────────────────────────
        if len(history) > 10:
            summary = summarize_history(history[:-6])
            history = [
                {"role": "system",
                 "content": f"Previous conversation summary: {summary}"}
            ] + history[-6:]

        history_str = "\n".join(
            [f"{m['role'].upper()}: {m['content']}" for m in history]
        )

        # ── 6. Build final prompt ──────────────────────────────
        final_system_prompt = system_prompt.format(
            retrieved_docs=context_str,
            chat_history=history_str,
            student_context=student_context,   # ← new placeholder
        )

        messages = [SystemMessage(content=final_system_prompt)]

        if image_data:
            content_block = [
                {"type": "text", "text": user_text},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
            ]
            messages.append(HumanMessage(content=content_block))
        else:
            messages.append(HumanMessage(content=user_text))

        response = chatModel.invoke(messages)

        new_history = history + [
            {"role": "user",      "content": user_text + (" [Image Uploaded]" if image_data else "")},
            {"role": "assistant", "content": response.content},
        ]

        return {"answer": response.content, "chat_history": new_history}

    graph.add_node("llm", call_llm)
    graph.set_entry_point("llm")
    graph.add_edge("llm", END)
    return graph.compile(checkpointer=InMemorySaver())


app_graph = create_graph()