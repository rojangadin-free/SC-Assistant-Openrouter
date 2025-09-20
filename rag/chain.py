from langchain_pinecone import PineconeVectorStore
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict
from pinecone import Pinecone
from langchain.schema import Document
# CORRECTED: Import Ranker and RerankRequest
from flashrank import Ranker, RerankRequest
from src.helper import get_local_embeddings
from src.prompt import system_prompt
from config import INDEX_NAME, CHAT_MODEL_NAME, SUMMARIZER_MODEL_NAME, OPENROUTER_API_KEY, PINECONE_API_KEY

# ====== Vector Store ======
embeddings = get_local_embeddings()
pinecone = Pinecone(api_key=PINECONE_API_KEY)
index = pinecone.Index(INDEX_NAME)
docsearch = PineconeVectorStore(index, embeddings)
# Retrieve 7 documents initially for re-ranking
retriever = docsearch.as_retriever(search_type="similarity", search_kwargs={"k": 15})

# ====== Re-ranker ======
reranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2")

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
        {"role": "system", "content": "Summarize the following conversation. Keep it concise but preserve key facts, questions, and answers."},
        {"role": "user", "content": transcript}
    ]
    response = summarizer.invoke(summary_prompt)
    return response.content

# ====== LangGraph Setup ======
def create_graph():
    """Creates and compiles the LangGraph."""
    graph = StateGraph(ChatState)

    def call_llm(state: ChatState):
        # 1. Initial retrieval from Pinecone
        initial_docs = retriever.invoke(state["input"])
        
        # 2. Re-ranking with FlashRank
        if initial_docs:
            passages = [{"id": i, "text": doc.page_content, "meta": doc.metadata} for i, doc in enumerate(initial_docs)]
            
            # CORRECTED: Create a RerankRequest object
            rerank_request = RerankRequest(query=state["input"], passages=passages)
            
            # Pass the single request object and slice the result
            reranked_passages = reranker.rerank(rerank_request)[:4]
            
            reranked_docs = [Document(page_content=p["text"], metadata=p["meta"]) for p in reranked_passages]
        else:
            reranked_docs = []

        # 3. Use the re-ranked documents for context
        retrieved_docs_context = docs_to_context(reranked_docs)

        history_for_state = state.get("chat_history", [])
        history_for_llm = list(history_for_state)

        if len(history_for_llm) > 20:
            summary = summarize_history(history_for_llm[:-5])
            history_for_llm = [{"role": "system", "content": f"This is a summary of the preceding conversation: {summary}"}] + history_for_llm[-5:]

        final_system_prompt = system_prompt.format(
            retrieved_docs=retrieved_docs_context,
            chat_history=""
        )
        final_messages_for_llm = [("system", final_system_prompt)]
        for msg in history_for_llm:
            final_messages_for_llm.append((msg['role'], msg['content']))
        final_messages_for_llm.append(("human", state["input"]))

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