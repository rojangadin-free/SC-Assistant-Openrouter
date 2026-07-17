import os
import requests
import concurrent.futures
from datetime import datetime
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict, Optional
from pinecone import Pinecone

from langchain_community.retrievers import PineconeHybridSearchRetriever
from langchain_classic.retrievers import EnsembleRetriever
from pinecone_text.sparse import BM25Encoder

from src.helper import get_local_embeddings
from src.prompt import system_prompt
from config import (
    INDEX_NAME, CHAT_MODEL_NAME, FALLBACK_MODEL_NAME, SUMMARIZER_MODEL_NAME,
    PINECONE_API_KEY, OPENROUTER_API_KEY
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

# ⚡ OPTIMIZATION: Reduced top_k from 20 to 8 to avoid RRF data-sprawl latency
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
    weights=[0.4, 0.6],
)

# ⚡ OPTIMIZATION: Shifted from heavy local CPU CrossEncoder to ultra-fast OpenRouter Reranker
RERANK_MODEL = "nvidia/llama-nemotron-rerank-vl-1b-v2:free"
RERANK_TOP_K = 5  # ⚡ OPTIMIZATION: Reduced from 15 to 8 to maximize LLM generation speed


def rerank_docs(query: str, docs: list) -> list:
    if not docs:
        return docs
        
    doc_texts = [doc.page_content for doc in docs]
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://samarcollege.edu.ph",
        "X-Title": "SC-Assistant"
    }
    payload = {
        "model": RERANK_MODEL,
        "query": query,
        "documents": doc_texts,
        "top_n": RERANK_TOP_K  # Since boosting is removed, we just request the exact Top K needed
    }
    
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/rerank",
            headers=headers,
            json=payload,
            timeout=6
        )
        response.raise_for_status()
        api_results = response.json().get("results", [])
        
        # Map returned indices back to the original documents
        top_docs = []
        for res in api_results:
            doc_idx = res["index"]
            if doc_idx < len(docs):
                top_docs.append(docs[doc_idx])
                
        print(f"Reranked via OpenRouter {len(docs)} → top {len(top_docs)} docs")
        return top_docs
        
    except Exception as e:
        print(f"OpenRouter Reranking API failed (falling back to initial order): {e}")
        return docs[:RERANK_TOP_K]


# --- MODEL INSTANTIATION ---
primary_model = ChatOpenAI(
    model=CHAT_MODEL_NAME,
    openai_api_key=OPENROUTER_API_KEY,
    openai_api_base="https://openrouter.ai/api/v1",
    temperature=0.2,
    timeout=30,
)

fallback_model = ChatOpenAI(
    model=FALLBACK_MODEL_NAME,
    openai_api_key=OPENROUTER_API_KEY,
    openai_api_base="https://openrouter.ai/api/v1",
    temperature=0.3,
    timeout=30,
)

chatModel = primary_model.with_fallbacks([fallback_model])

summarizer = ChatOpenAI(
    model=SUMMARIZER_MODEL_NAME,
    openai_api_key=OPENROUTER_API_KEY,
    openai_api_base="https://openrouter.ai/api/v1",
    temperature=0,
    timeout=30,
)

# ====== Chat State ======
class ChatState(TypedDict):
    input: str
    image_data: Optional[str]
    chat_history: List[Dict[str, str]]
    answer: str
    uid: Optional[str]
    user_email: Optional[str]
    data_consent: Optional[bool]
    messages_to_llm: Optional[list]


def docs_to_context(docs) -> str:
    context_parts = []
    for i, d in enumerate(docs):
        source = d.metadata.get("source", "Unknown")
        page   = d.metadata.get("page", "?")
        
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

            # 🚀 PARALLEL TASK 1: Image Analysis
            def task_image_analysis():
                if not image_data: return None
                vision_msg = HumanMessage(content=[
                    {"type": "text", "text": "Transcribe any text in this image and describe the visual layout in detail."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                ])
                try:
                    desc = summarizer.invoke([vision_msg]).content
                    print(f"Image Analysis: {desc}")
                    return desc
                except Exception as e:
                    print(f"Image analysis failed (non-fatal): {e}")
                    return None

            # 🚀 PARALLEL TASK 2: DynamoDB Student Record Fetch
            def task_student_fetch():
                if not uid: return ""
                try:
                    # Always fetch the record so the system knows WHO they are
                    student = get_student_by_uid(uid)
                    if not student and user_email:
                        student = get_student_by_email(user_email)
                        
                    if student:
                        # If user disabled data consent, provide ONLY basic academic context for personalization
                        if state.get("data_consent") is False:
                            print("  Data consent disabled. Providing basic profile only.")
                            return (
                                "System Note: The user has explicitly opted out of sharing their personal student data. "
                                "Do not provide specific grades, balances, or schedules."
                                "The user has account so he/she is a continuing student already but no further details are available due to privacy settings. "
                                f"Name: {student.get('full_name')}, "
                            )
                        
                        # Full context if consent is enabled
                        print(f"  Student record fetched: {student.get('full_name')} | balance: PHP {student.get('balance', 'N/A')}")
                        return format_student_context(student)
                    else:
                        print("  No student record found for this user.")
                        return f"System Note: User is logged in as {user_email} but no record was found."
                except Exception as e:
                    print(f"  Student record fetch failed (non-fatal): {e}")
                    return ""

            # 🚀 PARALLEL TASK 3: Query Optimization
            def task_query_optimization():
                try:
                    recent_history = "\n".join([f"{m['role'].title()}: {m['content']}" for m in history[-6:]]) if history else "No previous history."
                    context_prompt = [
                        {
                            "role": "system", 
                            "content": (
                                "You are a search query optimizer. Understand the user's intent and the provided context first. Formulate the best search query for RAG that uses sparse and dense retrieval. If you cannot understand the user's intent, return the user's original query.\n"
                                "CRITICAL SEARCH RULES: \n"
                                "1. CONTEXT AWARENESS: Only check chat history if the user asks and it is not a complete question.\n"
                                "2. Do NOT answer the question, just output the optimized search query."
                            )
                        },
                        {"role": "user", "content": f"Chat History:\n{recent_history}\n\nLatest Question: {user_text}"}
                    ]
                    
                    summary_resp = summarizer.invoke(context_prompt).content
                    
                    if isinstance(summary_resp, list):
                        summary_resp = "".join([
                            part.get("text", "") if isinstance(part, dict) else str(part)
                            for part in summary_resp
                        ])
                    elif not isinstance(summary_resp, str):
                        summary_resp = str(summary_resp)
                        
                    q = summary_resp.strip()
                    print(f"  Contextualized Query: {q}")
                    return q
                except Exception as e:
                    print(f"  Contextualization failed (non-fatal): {e}")
                    return user_text

            # 🚀 EXECUTE ALL 3 TASKS SIMULTANEOUSLY
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                future_img     = executor.submit(task_image_analysis)
                future_student = executor.submit(task_student_fetch)
                future_query   = executor.submit(task_query_optimization)

                # Wait for them to finish and collect results instantly
                image_description = future_img.result()
                student_context   = future_student.result()
                standalone_query  = future_query.result()

            # === STEP 1: Retrieval ===
            print(f"\n=== STEP 1: Retrieval (Query: '{standalone_query}') ===")
            try:
                initial_docs = retriever.invoke(standalone_query)
                print(f"  RRF returned {len(initial_docs)} candidates")
            except Exception as e:
                print(f"  Retrieval failed (non-fatal): {e}")
                initial_docs = []

            # === STEP 2: Reranking ===
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

            # === STEP 3: Handle Conversation Summary ===
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
                current_date=datetime.now().strftime("%B %d, %Y")
            )

            # === STEP 4: Assemble Messages for Flask Streaming ===
            messages = [SystemMessage(content=final_system_prompt)]

            if image_data:
                # 🚀 Inject the parallel-processed image description so the LLM is aware of it
                augmented_text = user_text
                if image_description:
                    augmented_text += f"\n\n[System Note - Image Analysis Provided]: {image_description}"

                content_block = [
                    {"type": "text", "text": augmented_text}, 
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                ]
                messages.append(HumanMessage(content=content_block))
            else:
                messages.append(HumanMessage(content=user_text))

            # Pass compiled state back out to Flask to generate the SSE tokens
            return {"messages_to_llm": messages, "chat_history": history}

        except Exception as e:
            import traceback
            print(f"[chain] Unhandled error: {traceback.format_exc()}")
            return {
                "messages_to_llm": [],
                "chat_history": state.get("chat_history", []),
            }

    graph.add_node("llm", call_llm)
    graph.set_entry_point("llm")
    graph.add_edge("llm", END)
    return graph.compile(checkpointer=InMemorySaver())

app_graph = create_graph()