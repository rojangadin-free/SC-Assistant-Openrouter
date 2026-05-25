import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict, Optional
from pinecone import Pinecone

from langchain_community.retrievers import PineconeHybridSearchRetriever
from langchain_classic.retrievers import EnsembleRetriever
from pinecone_text.sparse import BM25Encoder
from sentence_transformers import CrossEncoder

from src.helper import get_local_embeddings
from src.prompt import system_prompt
from config import (
    INDEX_NAME, CHAT_MODEL_NAME, FALLBACK_MODEL_NAME, SUMMARIZER_MODEL_NAME,
    PINECONE_API_KEY, VERTEX_EXPRESS_API_KEY
)

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
    
    boosted_ranked = []
    for score, doc in zip(scores, docs):
        source = doc.metadata.get("source", "").lower()
        
        # 🚀 MATHEMATICAL BOOST FOR NEW DOCUMENTS
        # If the document is NOT the base 2024 handbook, we give its relevance score 
        # a massive artificial boost (+15.0) so it mathematically destroys the 
        # old handbook in the ranking algorithm and jumps to the top of the AI's memory.
        if "samar-college-2024.pdf" not in source:
            score += 15.0 
            
        boosted_ranked.append((score, doc))
        
    ranked = sorted(boosted_ranked, key=lambda x: x[0], reverse=True)
    top    = [doc for _, doc in ranked[:RERANK_TOP_K]]
    print(f"  Reranked {len(docs)} → top {len(top)} docs")
    return top


# --- VERTEX AI EXPRESS MODE INSTANTIATION ---

# 1. The Main Model (Attempts to answer within 30 seconds)
primary_model = ChatGoogleGenerativeAI(
    model=CHAT_MODEL_NAME,
    api_key=VERTEX_EXPRESS_API_KEY,
    vertexai=True,
    temperature=0.3,
    max_retries=0,   # CRITICAL: Set to 0 so it immediately falls back on timeout
    timeout=30.0,    # 30-second strict cutoff
)

# 2. The Fallback Model (Fast model to save the day)
fallback_model = ChatGoogleGenerativeAI(
    model=FALLBACK_MODEL_NAME, 
    api_key=VERTEX_EXPRESS_API_KEY,
    vertexai=True,
    temperature=0.3,
    max_retries=2,   # The fallback is allowed to retry if there's a network blip
)

# 3. Combine them using LangChain's built-in fallback router
chatModel = primary_model.with_fallbacks([fallback_model])

# (Leave your summarizer below exactly as it is)
summarizer = ChatGoogleGenerativeAI(
    model=SUMMARIZER_MODEL_NAME,
    api_key=VERTEX_EXPRESS_API_KEY,
    vertexai=True, 
    temperature=0,
)

# ====== Chat State ======
class ChatState(TypedDict):
    input: str
    image_data: Optional[str]
    chat_history: List[Dict[str, str]]
    answer: str
    uid: Optional[str]
    user_email: Optional[str]


def docs_to_context(docs) -> str:
    """Format retrieved docs with clear citation anchors and priority tags."""
    context_parts = []
    for i, d in enumerate(docs):
        source = d.metadata.get("source", "Unknown")
        page   = d.metadata.get("page", "?")
        
        # 🚀 INJECT PRIORITY TAGS FOR THE AI
        is_base = "samar-college-2024.pdf" in source.lower()
        priority_tag = "" if is_base else " [🚨 NEW UPDATE - OVERRIDE BASE KNOWLEDGE]"
        
        text   = (
            f"[DOCUMENT {i + 1}]{priority_tag}\n"
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


def safe_prompt(template: str, **kwargs) -> str:
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", str(value) if value is not None else "")
    return result


# ====== LangGraph ======
def create_graph():
    graph = StateGraph(ChatState)

    def call_llm(state: ChatState):
        try:
            user_text  = state["input"]
            image_data = state.get("image_data")
            history    = state.get("chat_history", [])
            uid        = state.get("uid")
            user_email = state.get("user_email")

            if image_data:
                vision_msg = HumanMessage(content=[
                    {"type": "text",
                     "text": "Transcribe any text in this image and describe the visual layout in detail."},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                ])
                try:
                    image_description = summarizer.invoke([vision_msg]).content
                    print(f"Image Analysis: {image_description}")
                except Exception as e:
                    print(f"Image analysis failed (non-fatal): {e}")

            student_context = ""
            if uid:
                try:
                    student = get_student_by_uid(uid)
                    if not student and user_email:
                        student = get_student_by_email(user_email)
                    if student:
                        print(f"  Student record fetched: {student.get('full_name')} "
                              f"| balance: PHP {student.get('balance', 'N/A')}")
                        student_context = format_student_context(student)
                    else:
                        print("  No student record found for this user.")
                except Exception as e:
                    print(f"  Student record fetch failed (non-fatal): {e}")

            standalone_query = user_text
            if history:
                try:
                    recent_history = "\n".join([f"{m['role'].title()}: {m['content']}" for m in history[-6:]])
                    context_prompt = [
                        {"role": "system", "content": "Given a chat history and the latest user question which might reference context in the chat history, formulate a standalone search query that can be understood without the chat history. Do NOT answer the question, just reformulate it if needed and otherwise return it exactly as is."},
                        {"role": "user", "content": f"Chat History:\n{recent_history}\n\nLatest Question: {user_text}"}
                    ]
                    standalone_query = summarizer.invoke(context_prompt).content.strip()
                    print(f"  Contextualized Query: {standalone_query}")
                except Exception as e:
                    print(f"  Contextualization failed (non-fatal): {e}")
                    standalone_query = user_text

            print(f"\n=== STEP 1: Retrieval (Query: '{standalone_query}') ===")
            try:
                initial_docs = retriever.invoke(standalone_query)
                print(f"  RRF returned {len(initial_docs)} candidates")
            except Exception as e:
                print(f"  Retrieval failed (non-fatal): {e}")
                initial_docs = []

            print(f"\n=== STEP 2: Reranking to top {RERANK_TOP_K} ===")
            try:
                reranked_docs = rerank_docs(standalone_query, initial_docs)
                for i, doc in enumerate(reranked_docs):
                    src     = doc.metadata.get("source", "Unknown")
                    pg      = doc.metadata.get("page", "?")
                    snippet = doc.page_content.replace("\n", " ")[:80]
                    print(f"  [{i+1}] {src} (Pg {pg}): {snippet}...")
            except Exception as e:
                print(f"  Reranking failed (non-fatal): {e}")
                reranked_docs = initial_docs[:RERANK_TOP_K]

            context_str = docs_to_context(reranked_docs)

            if len(history) > 10:
                try:
                    summary = summarize_history(history[:-6])
                    history = [
                        {"role": "system",
                         "content": f"Previous conversation summary: {summary}"}
                    ] + history[-6:]
                except Exception as e:
                    print(f"  History summarization failed (non-fatal): {e}")
                    history = history[-6:]

            history_str = "\n".join(
                [f"{m['role'].upper()}: {m['content']}" for m in history]
            )

            final_system_prompt = safe_prompt(
                system_prompt,
                retrieved_docs=context_str,
                chat_history=history_str,
                student_context=student_context,
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

            # --- FIX FOR GEMINI 3.1 CONTENT BLOCKS ---
            # Extract the string regardless of whether it's a string (2.5) or a list of blocks (3.1)
            answer_text = response.content
            if isinstance(answer_text, list):
                answer_text = "".join(
                    block.get("text", "") for block in answer_text 
                    if isinstance(block, dict) and block.get("type") == "text"
                )

            new_history = history + [
                {"role": "user",      "content": user_text + (" [Image Uploaded]" if image_data else "")},
                {"role": "assistant", "content": answer_text},
            ]

            return {"answer": answer_text, "chat_history": new_history}

        except Exception as e:
            import traceback
            print(f"[chain] Unhandled error: {traceback.format_exc()}")
            return {
                "answer": "I'm sorry, I encountered an issue processing your request. Please try rephrasing your question.",
                "chat_history": state.get("chat_history", []),
            }

    graph.add_node("llm", call_llm)
    graph.set_entry_point("llm")
    graph.add_edge("llm", END)
    return graph.compile(checkpointer=InMemorySaver())

app_graph = create_graph()