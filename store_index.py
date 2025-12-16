import os
import re
import time
import io
import zipfile
import logging
import tempfile
from typing import List, Optional, Iterable, Tuple

import fitz  # PyMuPDF
import pdfplumber
import docx
from docx.document import Document as _Document
from docx.oxml.text.paragraph import CT_P
from docx.oxml.table import CT_Tbl
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph

from PIL import Image, ImageOps
from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec
from langchain_pinecone import PineconeVectorStore
from langchain_core.documents import Document
from langchain_community.document_loaders import (
    TextLoader,
    UnstructuredMarkdownLoader,
    CSVLoader,
)

from src.helper import (
    smart_chunking,
    get_local_embeddings,
    generate_image_caption,
    clean_text,
    generate_chunk_id,
    is_low_value_chunk,
)

from aws.s3 import upload_file_to_s3

logger = logging.getLogger(__name__)

load_dotenv()
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
if not PINECONE_API_KEY:
    raise RuntimeError("PINECONE_API_KEY not set in environment.")


# ============================================================
# METADATA SANITIZER
# ============================================================
def clean_metadata(doc: Document) -> Document:
    cleaned = {}
    for k, v in (doc.metadata or {}).items():
        if k in {"image_base64", "base64_image", "orig_elements", "coordinates"}:
            continue
        if isinstance(v, str) and len(v) > 2048:
            continue
        cleaned[k] = v
    doc.metadata = cleaned
    return doc


# ============================================================
# HELPERS
# ============================================================
def get_embedding_dimension(embeddings) -> int:
    vec = embeddings.embed_query("dimension probe")
    return len(vec)


def batched(items: List[Document], n: int) -> Iterable[List[Document]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def text_quality(text: str) -> Tuple[int, int, float]:
    """
    Returns: (char_len, word_count, alpha_ratio)
    alpha_ratio helps detect "mostly symbols/whitespace" extraction.
    """
    t = (text or "").strip()
    if not t:
        return 0, 0, 0.0

    char_len = len(t)
    words = re.findall(r"[A-Za-z0-9]+", t)
    word_count = len(words)

    alpha = sum(1 for c in t if c.isalpha())
    alpha_ratio = alpha / max(1, char_len)
    return char_len, word_count, alpha_ratio


def should_ocr_extracted_text(extracted: str, *, min_chars: int, min_words: int, min_alpha_ratio: float) -> bool:
    char_len, word_count, alpha_ratio = text_quality(extracted)
    if char_len < min_chars:
        return True
    if word_count < min_words:
        return True
    if alpha_ratio < min_alpha_ratio:
        return True
    return False


# ============================================================
# PDF EXTRACTION
# ============================================================
def extract_text_from_page(plumber_page, fitz_page) -> str:
    text = ""
    try:
        text = plumber_page.extract_text(x_tolerance=2) or ""
    except Exception:
        text = ""

    if not text.strip():
        try:
            text = fitz_page.get_text("text") or ""
        except Exception:
            text = ""

    return clean_text(text)


def render_page_image(fitz_page, zoom: float = 3.0) -> Image.Image:
    """
    Higher zoom improves OCR for small text (like load sheets).
    """
    mat = fitz.Matrix(zoom, zoom)
    pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    # light cleanup for OCR
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    return img


def ocr_page_image(image: Image.Image) -> str:
    return generate_image_caption(
        image,
        prompt="Transcribe all visible text exactly as-is. Preserve tables/columns as best as possible. Do not summarize.",
    )


def process_pdf(filepath: str, filename: str) -> List[Document]:
    docs: List[Document] = []

    with fitz.open(filepath) as fdoc, pdfplumber.open(filepath) as pdoc:
        total_pages = len(fdoc)
        logger.info(f"Processing PDF: {filename} | Pages: {total_pages}")

        for page_idx in range(total_pages):
            fitz_page = fdoc.load_page(page_idx)
            plumber_page = pdoc.pages[page_idx] if page_idx < len(pdoc.pages) else None

            page_num = page_idx + 1
            extracted = (
                extract_text_from_page(plumber_page, fitz_page)
                if plumber_page
                else clean_text(fitz_page.get_text("text"))
            )

            # Better OCR trigger: small forms often have SOME extracted text but not enough.
            try:
                words_on_page = fitz_page.get_text("words") or []
                word_boxes = len(words_on_page)
            except Exception:
                word_boxes = 0

            ocr_needed = should_ocr_extracted_text(
                extracted,
                min_chars=180,
                min_words=40,
                min_alpha_ratio=0.12,
            )

            # If page has very few extracted word boxes, OCR is usually required.
            if word_boxes < 25:
                ocr_needed = True

            if ocr_needed:
                try:
                    img = render_page_image(fitz_page, zoom=3.5)
                    page_text = clean_text(ocr_page_image(img))
                    page_type = "pdf_ocr"
                except Exception as e:
                    logger.warning(f"OCR failed for {filename} p{page_num}: {type(e).__name__}: {e}")
                    page_text = extracted
                    page_type = "pdf"
            else:
                page_text = extracted
                page_type = "pdf"

            # Table extraction only if page is text-rich (kept as-is)
            if len(extracted.strip()) >= 200:
                try:
                    tables = fitz_page.find_tables()
                except Exception:
                    tables = None

                if tables:
                    for t_i, table in enumerate(tables):
                        tmp_path = None
                        try:
                            pix = fitz_page.get_pixmap(clip=table.bbox, matrix=fitz.Matrix(3, 3), alpha=False)
                            image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")

                            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                                tmp_path = tmp.name
                                image.save(tmp_path)

                            s3_key = f"tables/{filename}_p{page_num}_t{t_i}.png"
                            if upload_file_to_s3(tmp_path, s3_key):
                                table_md = generate_image_caption(
                                    image,
                                    prompt="Extract this table as a precise Markdown table. Keep numbers exactly.",
                                )
                                page_text += f"\n\n[TABLE]\n{table_md}\n"
                        finally:
                            try:
                                if tmp_path and os.path.exists(tmp_path):
                                    os.remove(tmp_path)
                            except Exception:
                                pass

            if page_text.strip():
                docs.append(
                    Document(
                        page_content=page_text,
                        metadata={"source": filename, "page": page_num, "type": page_type},
                    )
                )

    return docs


# ============================================================
# DOCX LINEAR PROCESSING (FIXED)
# ============================================================

def iter_block_items(parent):
    """
    Yield each paragraph and table child within *parent*, in document order.
    """
    if isinstance(parent, _Document):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        raise ValueError("something's not right")

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def get_images_from_paragraph(paragraph, doc_obj):
    """
    Extracts images anchored in a paragraph.
    """
    images = []
    # Loop through runs to find drawing elements (blips)
    for run in paragraph.runs:
        if 'w:drawing' in run.element.xml:
            # Parse XML for blip (image reference)
            drawing_xml = run.element.xml
            if 'r:embed="' in drawing_xml:
                try:
                    # Naive extraction of rId
                    rId = drawing_xml.split('r:embed="')[1].split('"')[0]
                    if rId in doc_obj.part.related_parts:
                        image_part = doc_obj.part.related_parts[rId]
                        image_bytes = image_part.blob
                        images.append(image_bytes)
                except Exception:
                    pass
    return images


def process_docx_linear(filepath: str, filename: str) -> List[Document]:
    """
    Reads DOCX linearly. Preserves order of Text -> Image -> Table.
    Updated: Captures context (headers) and attaches it to images/tables to prevent RAG mix-ups.
    """
    logger.info(f"📖 Linear processing for DOCX: {filename}")
    try:
        doc = docx.Document(filepath)
    except Exception as e:
        logger.error(f"Failed to open DOCX {filename}: {e}")
        return []

    full_text = []
    
    # Keep track of the last few lines of text to use as context for images
    context_buffer = []

    for block in iter_block_items(doc):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                full_text.append(text)
                # Update context buffer (keep last 3 non-empty lines)
                context_buffer.append(text)
                if len(context_buffer) > 3:
                    context_buffer.pop(0)
            
            # Check for images in this paragraph
            images = get_images_from_paragraph(block, doc)
            for i, img_bytes in enumerate(images):
                try:
                    image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                    logger.info(f"   📷 OCRing inline image in {filename}...")
                    
                    # Create a context string from the buffer
                    context_str = " | ".join(context_buffer)
                    
                    caption = generate_image_caption(
                        image, 
                        # Inject context into the prompt so Gemini knows what it's looking at
                        prompt=f"Context: {context_str}. Transcribe this image content exactly. If it is a table, format it as Markdown. Do not summarize."
                    )
                    if caption:
                        # Explicitly label the image content with the context in the final text
                        # We use '***' to help the chunker see a semantic break
                        full_text.append(f"\n***\n[SECTION CONTEXT: {context_str}]\n[IMAGE CONTENT]\n{caption}\n***\n")
                except Exception as e:
                    logger.warning(f"Failed to process inline image in {filename}: {e}")

        elif isinstance(block, Table):
            rows_data = []
            for row in block.rows:
                row_cells = [cell.text.strip() for cell in row.cells]
                rows_data.append(" | ".join(row_cells))
            
            if rows_data:
                # Also attach context to standard tables
                context_str = " | ".join(context_buffer)
                table_text = "\n".join(rows_data)
                full_text.append(f"\n***\n[SECTION CONTEXT: {context_str}]\n[TABLE]\n{table_text}\n***\n")

    combined_content = "\n\n".join(full_text)
    
    if not combined_content.strip():
        return []

    return [Document(
        page_content=combined_content,
        metadata={"source": filename, "type": "docx_linear"}
    )]


# ============================================================
# LOADERS
# ============================================================
def get_loader(filepath: str):
    ext = os.path.splitext(filepath)[1].lower()
    # Note: .docx is now handled by process_docx_linear directly
    if ext == ".txt":
        return TextLoader(filepath)
    if ext == ".md":
        return UnstructuredMarkdownLoader(filepath)
    if ext == ".csv":
        return CSVLoader(filepath, encoding="utf-8")
    return None


# ============================================================
# INDEX MANAGEMENT
# ============================================================
def ensure_index_exists(pc: Pinecone, index_name: str, dimension: int):
    if index_name not in pc.list_indexes().names():
        pc.create_index(
            name=index_name,
            dimension=dimension,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
        while not pc.describe_index(index_name).status["ready"]:
            time.sleep(1)


# ============================================================
# BUILD / APPEND
# ============================================================

def build_index(data_path: str = "data/", index_name: str = "rag-google-v5", embeddings=None):
    if embeddings is None:
        raise ValueError("Embeddings required")

    dim = get_embedding_dimension(embeddings)
    pc = Pinecone(api_key=PINECONE_API_KEY)
    ensure_index_exists(pc, index_name, dimension=dim)

    raw_docs: List[Document] = []

    for fname in os.listdir(data_path):
        path = os.path.join(data_path, fname)
        if not os.path.isfile(path):
            continue

        lower = fname.lower()
        if lower.endswith(".pdf"):
            raw_docs.extend(process_pdf(path, fname))

        elif lower.endswith(".docx"):
            # Use the new linear processing for DOCX
            raw_docs.extend(process_docx_linear(path, fname))

        else:
            loader = get_loader(path)
            if loader:
                docs = loader.load()
                for d in docs:
                    d.metadata["source"] = fname
                    clean_metadata(d)
                raw_docs.extend(docs)

    final_chunks: List[Document] = []
    seen_ids = set()

    for doc in raw_docs:
        clean_metadata(doc)
        chunks = smart_chunking(doc.page_content)

        for idx, text in enumerate(chunks):
            if is_low_value_chunk(text):
                continue

            cid = generate_chunk_id(
                doc.metadata.get("source", ""),
                int(doc.metadata.get("page", 0) or 0),
                idx,
                text,
            )
            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            meta = dict(doc.metadata)
            meta["chunk_index"] = idx
            meta["chunk_id"] = cid

            final_chunks.append(Document(page_content=text, metadata=meta))

    logger.info(f"Upserting chunks: {len(final_chunks)}")

    if final_chunks:
        vectorstore = PineconeVectorStore.from_existing_index(
            index_name=index_name,
            embedding=embeddings,
        )
        for batch in batched(final_chunks, 128):
            vectorstore.add_documents(batch)

    logger.info("Index build complete")


def append_file_to_index(
    filepath: str,
    index_name: str = "rag-google-v5",
    real_name: Optional[str] = None,
    embeddings=None
) -> int:
    if embeddings is None:
        raise ValueError("Embeddings required")

    filename = real_name if real_name else os.path.basename(filepath)

    dim = get_embedding_dimension(embeddings)
    pc = Pinecone(api_key=PINECONE_API_KEY)
    ensure_index_exists(pc, index_name, dimension=dim)

    raw_docs: List[Document] = []

    lower = filename.lower()
    if lower.endswith(".pdf"):
        raw_docs = process_pdf(filepath, filename)

    elif lower.endswith(".docx"):
        # Use the new linear processing for DOCX
        raw_docs = process_docx_linear(filepath, filename)

    else:
        loader = get_loader(filepath)
        if not loader:
            raise ValueError(f"Unsupported file type: {filename}")
        raw_docs = loader.load()
        for d in raw_docs:
            d.metadata["source"] = filename
            clean_metadata(d)

    final_chunks: List[Document] = []
    seen_ids = set()

    for doc in raw_docs:
        clean_metadata(doc)
        chunks = smart_chunking(doc.page_content)

        for idx, text in enumerate(chunks):
            if is_low_value_chunk(text):
                continue

            cid = generate_chunk_id(
                doc.metadata.get("source", ""),
                int(doc.metadata.get("page", 0) or 0),
                idx,
                text,
            )
            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            meta = dict(doc.metadata)
            meta["chunk_index"] = idx
            meta["chunk_id"] = cid
            final_chunks.append(Document(page_content=text, metadata=meta))

    if final_chunks:
        vectorstore = PineconeVectorStore.from_existing_index(index_name=index_name, embedding=embeddings)
        for batch in batched(final_chunks, 128):
            vectorstore.add_documents(batch)

    logger.info(f"Appended chunks from {filename}: {len(final_chunks)}")
    return len(final_chunks)


if __name__ == "__main__":
    from config import INDEX_NAME
    logging.basicConfig(level=logging.INFO)
    embs = get_local_embeddings()
    build_index(index_name=INDEX_NAME, embeddings=embs)