import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict, Optional
from pinecone import Pinecone

# UPDATED IMPORTS: Added EnsembleRetriever
from langchain_community.retrievers import PineconeHybridSearchRetriever
from langchain.retrievers import EnsembleRetriever
from pinecone_text.sparse import BM25Encoder

from sentence_transformers import CrossEncoder

from src.helper import get_local_embeddings
from src.prompt import system_prompt
from config import INDEX_NAME, CHAT_MODEL_NAME, SUMMARIZER_MODEL_NAME, GOOGLE_API_KEY, PINECONE_API_KEY

# ====== Setup ======
embeddings = get_local_embeddings()
pinecone = Pinecone(api_key=PINECONE_API_KEY)
index = pinecone.Index(INDEX_NAME)

# --- 1. SETUP RETRIEVERS FOR RRF ---

# Load BM25 params
bm25_path = "bm25_values.json"
if os.path.exists(bm25_path):
    bm25 = BM25Encoder().load(bm25_path)
else:
    print("WARNING: bm25_values.json not found. Using default BM25.")
    bm25 = BM25Encoder().default()

# A. Sparse Retriever (Keyword Search only)
# alpha=0.0 means 100% sparse score
sparse_retriever = PineconeHybridSearchRetriever(
    embeddings=embeddings,
    sparse_encoder=bm25,
    index=index,
    top_k=10, 
    alpha=0.0 
)

# B. Dense Retriever (Semantic Search only)
# alpha=1.0 means 100% dense score
dense_retriever = PineconeHybridSearchRetriever(
    embeddings=embeddings,
    sparse_encoder=bm25, # Required by class structure, even if unused for scoring
    index=index,
    top_k=10,
    alpha=1.0
)

# C. Ensemble Retriever (Reciprocal Rank Fusion)
# Combines the results of both retrievers using RRF
retriever = EnsembleRetriever(
    retrievers=[sparse_retriever, dense_retriever],
    weights=[0.5, 0.5] # Equal weight to both strategies
)

# D. Cross-Encoder Reranker
# Scores query+doc pairs together — much more accurate than bi-encoder alone.
# Runs locally, no extra API cost. Reranks the RRF candidates down to top 6.
reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512)
RERANK_TOP_K = 8


def rerank_docs(query: str, docs: list) -> list:
    """Score each retrieved doc against the query jointly, keep only the best."""
    if not docs:
        return docs
    pairs = [(query, doc.page_content) for doc in docs]
    scores = reranker.predict(pairs)
    ranked = sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)
    top_docs = [doc for _, doc in ranked[:RERANK_TOP_K]]
    print(f"  Reranked {len(docs)} → top {len(top_docs)} docs")
    return top_docs

chatModel = ChatGoogleGenerativeAI(
    model=CHAT_MODEL_NAME,
    google_api_key=GOOGLE_API_KEY,
    temperature=0.3,  # Lower = more faithful to retrieved context, fewer hallucinations
    max_tokens=None,
    timeout=None,
    max_retries=2
)

summarizer = ChatGoogleGenerativeAI(
    model=SUMMARIZER_MODEL_NAME,
    google_api_key=GOOGLE_API_KEY,
    temperature=0
)

# ====== Chat State ======
class ChatState(TypedDict):
    input: str
    image_data: Optional[str]
    chat_history: List[Dict[str, str]]
    answer: str

def docs_to_context(docs) -> str:
    """
    Format retrieved docs with explicit [DOCUMENT N] headers so the LLM
    has unambiguous anchors to reference when generating [SOURCE:…] citations.
    """
    context_parts = []
    for i, d in enumerate(docs):
        source = d.metadata.get("source", "Unknown")
        page = d.metadata.get("page", "?")
        # Structured header gives the LLM a clear, parseable citation anchor
        text = (
            f"[DOCUMENT {i + 1}]\n"
            f"Source: {source} | Page: {page}\n"
            f"Content:\n{d.page_content}"
        )
        context_parts.append(text)
    if not context_parts:
        return "No relevant context found."
    return "\n\n---\n\n".join(context_parts)

def summarize_history(history: List[Dict[str, str]]) -> str:
    if not history: return ""
    transcript = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    summary_prompt = [
        {"role": "system", "content": "Summarize the conversation to retain key context."},
        {"role": "user", "content": transcript}
    ]
    return summarizer.invoke(summary_prompt).content

# ====== LangGraph ======
def create_graph():
    graph = StateGraph(ChatState)

    def call_llm(state: ChatState):
        user_text = state["input"]
        image_data = state.get("image_data")
        history = state.get("chat_history", [])
        
        # Direct user input without rewriting
        refined_query = user_text 

        # --- 1. Image Analysis (Vision) ---
        image_description = ""
        if image_data:
            vision_message = HumanMessage(content=[
                {"type": "text", "text": "Transcribe any text in this image and describe the visual layout in detail."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
            ])
            image_description = summarizer.invoke([vision_message]).content
            print(f"Image Analysis: {image_description}")

        # --- 2. Retrieval (Ensemble RRF) + Reranking ---
        print(f"\n=== 🔍 STEP 1: Retrieval (Query: '{refined_query}') ===")
        
        # Ensemble RRF: merges sparse (BM25) + dense (semantic) candidates
        initial_docs = retriever.invoke(refined_query)
        print(f"  RRF returned {len(initial_docs)} candidates")

        # Cross-encoder rerank: scores each doc against the query jointly
        print(f"\n=== ⚖️  STEP 2: Reranking to top {RERANK_TOP_K} ===")
        reranked_docs = rerank_docs(refined_query, initial_docs)
        
        # DEBUG: Print final docs sent to LLM
        for i, doc in enumerate(reranked_docs):
            src = doc.metadata.get("source", "Unknown")
            pg = doc.metadata.get("page", "?")
            snippet = doc.page_content.replace("\n", " ")[:80]
            print(f"  [{i+1}] {src} (Pg {pg}): {snippet}...")

        context_str = docs_to_context(reranked_docs)

        # --- 3. History Management ---
        if len(history) > 10:
            summary = summarize_history(history[:-6])
            history = [{"role": "system", "content": f"Previous conversation summary: {summary}"}] + history[-6:]

        history_str = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in history])

        # --- 4. Answer Generation ---
        final_system_prompt = system_prompt.format(
            retrieved_docs=context_str,
            chat_history=history_str 
        )

        messages = [SystemMessage(content=final_system_prompt)]
        
        if image_data:
            content_block = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
            ]
            messages.append(HumanMessage(content=content_block))
        else:
            messages.append(HumanMessage(content=user_text))

        response = chatModel.invoke(messages)

        new_history = history + [
            {"role": "user", "content": user_text + (" [Image Uploaded]" if image_data else "")},
            {"role": "assistant", "content": response.content}
        ]

        return {"answer": response.content, "chat_history": new_history}

    graph.add_node("llm", call_llm)
    graph.set_entry_point("llm")
    graph.add_edge("llm", END)
    return graph.compile(checkpointer=InMemorySaver())

app_graph = create_graph()