from langchain_pinecone import PineconeVectorStore
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict
from pinecone import Pinecone
from langchain.schema import Document
# Removed: from flashrank import Ranker, RerankRequest
from src.helper import get_local_embeddings
from src.prompt import system_prompt
from config import INDEX_NAME, CHAT_MODEL_NAME, SUMMARIZER_MODEL_NAME, OPENROUTER_API_KEY, PINECONE_API_KEY

# ====== Patch System Prompt ======
# We programmatically relax the prompt constraints to allow memory of user details.
# 1. Allow using history for facts (names, ages, etc.)
system_prompt = system_prompt.replace(
    "Reference solely for conversational continuity (e.g., pronouns or prior Samar College topics), never for new facts.",
    "Reference for conversational continuity and to recall personal details (name, age, preferences) provided by the user in the chat."
)
# 2. Allow personal topic questions if the answer is in the history
system_prompt = system_prompt.replace(
    "Reject non-Samar College topics immediately",
    "Reject non-Samar College topics (unless answering questions about the user's own information found in the history)"
)

# ====== Vector Store ======
embeddings = get_local_embeddings()
pinecone = Pinecone(api_key=PINECONE_API_KEY)
index = pinecone.Index(INDEX_NAME)
docsearch = PineconeVectorStore(index, embeddings)

# UPDATED: Retrieve only top 4 documents directly since we are not reranking
retriever = docsearch.as_retriever(search_type="similarity", search_kwargs={"k": 10})

# Removed: Reranker initialization

# ====== LLMs ======
chatModel = ChatOpenAI(
    model=CHAT_MODEL_NAME,
    openai_api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    temperature=0.7,
    max_tokens=2048
)

summarizer = ChatOpenAI(
    model=SUMMARIZER_MODEL_NAME,
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

def docs_to_context(docs) -> str:
    """Converts a list of documents to a string context."""
    if not docs:
        return "No relevant context found."
    return "\n\n".join([f"[{i+1}] {d.page_content}" for i, d in enumerate(docs)])

def summarize_history(history: List[Dict[str, str]]) -> str:
    """Summarizes the chat history."""
    if not history:
        return ""
    transcript = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    summary_prompt = [
        {"role": "system", "content": "Summarize the following conversation. Keep it concise but preserve key facts (like user's name, age) and questions."},
        {"role": "user", "content": transcript}
    ]
    response = summarizer.invoke(summary_prompt)
    return response.content

# ====== LangGraph Setup ======
def create_graph():
    """Creates and compiles the LangGraph."""
    graph = StateGraph(ChatState)

    def call_llm(state: ChatState):
        # 1. Retrieval from Pinecone
        retrieved_docs = retriever.invoke(state["input"])
        
        # 2. Use retrieved documents directly for context (No Reranking)
        retrieved_docs_context = docs_to_context(retrieved_docs)

        # 3. Process History
        history_for_state = state.get("chat_history", [])
        history_for_llm = list(history_for_state)

        # Summarization logic for long history (keep last 10 messages)
        if len(history_for_llm) > 10:
            summary = summarize_history(history_for_llm[:-10])
            history_for_llm = [{"role": "system", "content": f"Summary of previous conversation: {summary}"}] + history_for_llm[-10:]

        # Format history as string and inject into System Prompt
        history_str = ""
        if history_for_llm:
            # Format: "USER: message \n ASSISTANT: response"
            history_str = "\n".join([f"{msg['role'].upper()}: {msg['content']}" for msg in history_for_llm])
        else:
            history_str = "No previous conversation."

        # Inject the history string into the {chat_history} placeholder
        final_system_prompt = system_prompt.format(
            retrieved_docs=retrieved_docs_context,
            chat_history=history_str 
        )
        
        # Construct messages
        final_messages_for_llm = [
            ("system", final_system_prompt),
            ("human", state["input"])
        ]

        response = chatModel.invoke(final_messages_for_llm)

        updated_history = history_for_state + [
            {"role": "user", "content": state["input"]},
            {"role": "assistant", "content": response.content}
        ]
        return {"answer": response.content, "chat_history": updated_history}

    graph.add_node("llm", call_llm)
    graph.set_entry_point("llm")
    graph.add_edge("llm", END)
    return graph.compile(checkpointer=InMemorySaver())

app_graph = create_graph()