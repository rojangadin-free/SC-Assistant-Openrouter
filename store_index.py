# store_index.py

import os
import time
from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec
from langchain_pinecone import PineconeVectorStore
from langchain.schema import Document

# ✅ ADDED: Import new loaders for different file types
from langchain_community.document_loaders import (
    PyMuPDFLoader,
    UnstructuredWordDocumentLoader,
    TextLoader,
    UnstructuredMarkdownLoader,
    CSVLoader
)
from src.helper import text_split, get_local_embeddings, preprocess_documents


load_dotenv()
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
os.environ["PINECONE_API_KEY"] = PINECONE_API_KEY or ""
os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY or ""


# ✅ NEW: Dynamic loader selection based on file extension
def get_loader(filepath):
    """Selects and returns the appropriate document loader for a given file type."""
    _, extension = os.path.splitext(filepath)
    extension = extension.lower()

    loader_map = {
        ".pdf": PyMuPDFLoader,
        ".docx": UnstructuredWordDocumentLoader,
        ".txt": TextLoader,
        ".md": UnstructuredMarkdownLoader,
        ".csv": CSVLoader
    }

    loader_class = loader_map.get(extension)
    if not loader_class:
        print(f"⚠️ Unsupported file type: {extension}. Skipping file.")
        return None

    # Special handling for CSVLoader if needed
    if extension == ".csv":
        return CSVLoader(file_path=filepath, encoding="utf-8")

    return loader_class(filepath)


def build_index(data_path="data/", index_name="rag-database3", embeddings=None):
    if embeddings is None:
        raise ValueError("❌ embeddings must be provided to build_index")

    all_chunks = []
    # ✅ CHANGED: Loop through all files, not just PDFs
    for filename in os.listdir(data_path):
        filepath = os.path.join(data_path, filename)

        try:
            print(f"📂 Loading {filename}...")
            # ✅ CHANGED: Use the dynamic get_loader function
            loader = get_loader(filepath)
            if loader is None:
                continue # Skip unsupported file types

            docs = loader.load()

            # Set the source filename in metadata for each page/document
            for doc in docs:
                doc.metadata["source"] = filename

            print("✨ Pre-processing and cleaning text...")
            processed_docs = preprocess_documents(docs)

            chunks = text_split(processed_docs)
            all_chunks.extend(chunks)

        except Exception as e:
            print(f"❌ Error processing {filename}: {e}")
            continue

    pc = Pinecone(api_key=PINECONE_API_KEY)
    if index_name not in pc.list_indexes().names():
        print(f"⚡ Creating new index: {index_name}")
        pc.create_index(
            name=index_name,
            dimension=384,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )

    if not all_chunks:
        print("🤷 No documents to index. Exiting.")
        return

    batch_size = 50
    print(f"📌 Upserting {len(all_chunks)} chunks in batches of {batch_size}...")

    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i:i + batch_size]
        print(f"  -> Upserting batch {i//batch_size + 1}/{(len(all_chunks) + batch_size - 1)//batch_size}...")
        PineconeVectorStore.from_documents(
            documents=batch,
            index_name=index_name,
            embedding=embeddings,
        )
        print("     ...waiting 2 seconds before next batch.")
        time.sleep(2)

    print(f"✅ Rebuilt index with {len(all_chunks)} chunks.")


def append_file_to_index(filepath: str, index_name="rag-database3", real_name=None, embeddings=None):
    if embeddings is None:
        raise ValueError("❌ embeddings must be provided to append_file_to_index")

    filename = real_name if real_name else os.path.basename(filepath)

    try:
        print(f"📂 Loading {filename}...")
        # ✅ CHANGED: Use the dynamic get_loader function
        loader = get_loader(filepath)
        if loader is None:
            raise ValueError(f"Unsupported file type for {filename}")

        docs = loader.load()

        # Set the source filename in metadata for each page/document
        for doc in docs:
            doc.metadata["source"] = filename

        print("✨ Pre-processing and cleaning text...")
        processed_docs = preprocess_documents(docs)

        text_chunks = text_split(processed_docs)

        pc = Pinecone(api_key=PINECONE_API_KEY)
        if index_name not in pc.list_indexes().names():
            raise ValueError(f"Index {index_name} does not exist. Run build_index() first.")

        batch_size = 50
        print(f"🧠 Embedding and upserting {len(text_chunks)} chunks in batches of {batch_size}...")

        for i in range(0, len(text_chunks), batch_size):
            batch = text_chunks[i:i + batch_size]
            print(f"  -> Upserting batch {i//batch_size + 1}/{(len(text_chunks) + batch_size - 1)//batch_size}...")
            PineconeVectorStore.from_documents(
                documents=batch,
                index_name=index_name,
                embedding=embeddings,
            )
            print("     ...waiting 2 seconds before next batch.")
            time.sleep(2)

        print(f"✅ File {filename} appended to index with {len(text_chunks)} chunks.")
        return len(text_chunks)

    except Exception as e:
        print(f"❌ Error appending {filename}: {e}")
        # Re-raise the exception to be caught by the Flask route
        raise e


if __name__ == "__main__":
    embs = get_local_embeddings()
    build_index(embeddings=embs)