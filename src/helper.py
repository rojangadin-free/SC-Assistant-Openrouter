import os
import re
from langchain.text_splitter import RecursiveCharacterTextSplitter
# --- REMOVED ---
# from langchain_huggingface import HuggingFaceEndpointEmbeddings  # ✅ correct import
# +++ ADDED +++
from langchain_community.embeddings import SentenceTransformerEmbeddings

def preprocess_documents(docs):
    """Clean up document text."""
    for doc in docs:
        doc.page_content = re.sub(r"-\s+", "", doc.page_content)
        doc.page_content = re.sub(r"\n{2,}", "\n", doc.page_content)
        doc.page_content = re.sub(r"\s+", " ", doc.page_content).strip()
    return docs

def text_split(extracted_data):
    """Split text into chunks."""
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=150)
    return text_splitter.split_documents(extracted_data)

# --- RENAMED and CHANGED ---
# This function now loads a model that runs locally on the server's CPU.
# It will download and cache the model on the first run.
def get_local_embeddings():
    """Loads Sentence Transformer embeddings that run locally."""
    return SentenceTransformerEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )