import re
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema.document import Document
from langchain_huggingface import HuggingFaceEmbeddings

def preprocess_documents(docs):
    """Clean up document text."""
    for doc in docs:
        # This check ensures the document has page_content
        if hasattr(doc, 'page_content') and isinstance(doc.page_content, str):
            # Clean up hyphenated line breaks
            doc.page_content = re.sub(r"-\s+", "", doc.page_content)
            # Consolidate multiple newlines into one
            doc.page_content = re.sub(r"\n{2,}", "\n", doc.page_content)
            # Consolidate all whitespace into single spaces
            doc.page_content = re.sub(r"\s+", " ", doc.page_content).strip()
    return docs

def text_split(extracted_data):
    """Split text into chunks."""
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=150)
    return text_splitter.split_documents(extracted_data)

def get_local_embeddings():
    """
    Loads HuggingFace embeddings that run locally on the CPU.
    """
    # ⭐ UPDATED: Use the new HuggingFaceEmbeddings class
    # The model will be downloaded and cached on its first run.
    return HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        model_kwargs={'device': 'cpu'}
    )