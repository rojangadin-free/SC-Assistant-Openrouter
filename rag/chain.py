from langchain_pinecone import PineconeVectorStore
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict, Optional
from pinecone import Pinecone

from src.helper import get_local_embeddings
from src.prompt import system_prompt
from config import INDEX_NAME, CHAT_MODEL_NAME, SUMMARIZER_MODEL_NAME, GOOGLE_API_KEY, PINECONE_API_KEY

# ====== Setup ======
embeddings = get_local_embeddings()
pinecone = Pinecone(api_key=PINECONE_API_KEY)
index = pinecone.Index(INDEX_NAME)
docsearch = PineconeVectorStore(index, embeddings)

# 1. RETRIEVAL (Standard Semantic Search)
# Adjusted k to 10 since we are no longer filtering with a reranker
retriever = docsearch.as_retriever(search_type="similarity", search_kwargs={"k": 20})

chatModel = ChatGoogleGenerativeAI(
    model=CHAT_MODEL_NAME,
    google_api_key=GOOGLE_API_KEY,
    temperature=0.7,
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
    context_parts = []
    for i, d in enumerate(docs):
        # Include Source in the context passed to LLM
        source = d.metadata.get("source", "Unknown")
        page = d.metadata.get("page", "?")
        text = f"Source: {source} (Page {page}):\n{d.page_content}"
        context_parts.append(text)
    if not context_parts:
        return "No relevant context found."
    return "\n---\n".join(context_parts)

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

        # --- 2. Retrieval (Standard) ---
        print(f"\n=== 🔍 STEP 1: Retrieval (Query: '{refined_query}') ===")
        initial_docs = retriever.invoke(refined_query)
        
        # DEBUG: Print Retrieved Docs
        for i, doc in enumerate(initial_docs):
            src = doc.metadata.get("source", "Unknown")
            pg = doc.metadata.get("page", "?")
            snippet = doc.page_content.replace("\n", " ")[:80]
            print(f"  [{i+1}] {src} (Pg {pg}): {snippet}...")

        context_str = docs_to_context(initial_docs)

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