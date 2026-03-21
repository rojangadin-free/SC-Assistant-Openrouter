# src/helper.py
import re
import io
import base64
import hashlib
import logging
import time
from typing import List, Optional, Callable

from PIL import Image
from langchain_core.messages import HumanMessage
# Updated imports: Added HuggingFaceEmbeddings, kept ChatGoogleGenerativeAI
from langchain_huggingface import HuggingFaceEmbeddings 
from langchain_google_genai import ChatGoogleGenerativeAI

from config import GOOGLE_API_KEY, VISION_MODEL_NAME

logger = logging.getLogger(__name__)

# ============================================================
# CLEANING
# ============================================================

def remove_headers_footers(text: str) -> str:
    """
    Removes lines that look like page numbers or shouty headers.
    Heuristic: keep it conservative to avoid deleting real content.
    """
    lines = text.splitlines()
    cleaned = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        # page numbers only
        if re.fullmatch(r"\d{1,4}", s):
            continue
        # very short all-caps lines (common headers)
        if len(s) < 40 and s.isupper() and re.search(r"[A-Z]{3,}", s):
            continue
        cleaned.append(s)
    return "\n".join(cleaned)


def fix_hyphenation(text: str) -> str:
    """Fix word breaks like con-\\nnection -> connection."""
    return re.sub(r"-(\n|\r\n|\r)\s*([a-z])", r"\2", text)


def collapse_soft_line_breaks(text: str) -> str:
    """
    Preserve paragraphs, collapse single newlines inside paragraphs.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # normalize multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # collapse single newlines that are not paragraph separators
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return text


def clean_text(raw: str) -> str:
    """
    Master cleaner.
    Keeps paragraph breaks as \\n\\n (important for chunking).
    """
    if not raw:
        return ""

    t = raw.replace("\x00", "")  # defensive for some PDFs
    t = fix_hyphenation(t)
    t = remove_headers_footers(t)
    t = collapse_soft_line_breaks(t)

    # Normalize spaces but preserve paragraph breaks
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s+\n", "\n\n", t)
    return t.strip()


# ============================================================
# TOKEN + CHUNKING
# ============================================================

def approx_tokens(text: str) -> int:
    """
    Cheap, safer-than-words heuristic: ~4 chars/token.
    Not exact, but more stable across content than word-based ratios.
    """
    t = text.strip()
    if not t:
        return 0
    return max(1, int(len(t) / 4))


def semantic_split_sentences(text: str) -> List[str]:
    """
    Very lightweight sentence split.
    If you already use NLTK elsewhere, consider swapping this to sent_tokenize.
    """
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    out: List[str] = []
    for s in sents:
        s = s.strip()
        if not s:
            continue
        # attach very short fragments to previous sentence
        if len(s.split()) < 5 and out:
            out[-1] = (out[-1] + " " + s).strip()
        else:
            out.append(s)
    return out


def smart_chunking(
    text: str,
    max_tokens: int = 1000,
    overlap: int = 150,
    token_counter: Optional[Callable[[str], int]] = None,
) -> List[str]:
    """
    Hybrid chunking:
    - Clean text (preserve paragraphs)
    - Paragraph-first packing
    - Sentence fallback for large paragraphs
    - Always applies overlap between emitted chunks
    """
    text = clean_text(text)
    if not text:
        return []

    count = token_counter or approx_tokens
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]

    chunks: List[str] = []
    current_parts: List[str] = []
    current_tokens = 0

    def emit_current():
        nonlocal current_parts, current_tokens
        if not current_parts:
            return

        chunk = " ".join(current_parts).strip()
        if chunk:
            chunks.append(chunk)

        # build overlap from the end of current_parts
        if overlap > 0:
            keep: List[str] = []
            keep_tokens = 0
            for part in reversed(current_parts):
                keep.insert(0, part)
                keep_tokens += count(part)
                if keep_tokens >= overlap:
                    break
            current_parts = keep
            current_tokens = keep_tokens
        else:
            current_parts = []
            current_tokens = 0

    for para in paragraphs:
        para_tokens = count(para)

        if para_tokens > max_tokens:
            # sentence fallback
            for sent in semantic_split_sentences(para):
                sent_tokens = count(sent)
                if current_tokens + sent_tokens > max_tokens and current_parts:
                    emit_current()
                current_parts.append(sent)
                current_tokens += sent_tokens
            continue

        if current_tokens + para_tokens > max_tokens and current_parts:
            emit_current()

        current_parts.append(para)
        current_tokens += para_tokens

    # final flush (no need to overlap after last)
    if current_parts:
        chunk = " ".join(current_parts).strip()
        if chunk:
            chunks.append(chunk)

    return chunks


# ============================================================
# SAFETY HELPERS
# ============================================================

def generate_chunk_id(source: str, page: int, chunk_index: int, text: str) -> str:
    """
    Stable deterministic chunk ID.
    Use sha256 (sha1 is fine, but sha256 is standard and collision-resilient).
    """
    payload = f"{source}|{page}|{chunk_index}|{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_low_value_chunk(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if len(t) < 80:
        return True
    alpha = sum(c.isalpha() for c in t)
    if alpha / max(len(t), 1) < 0.35:
        return True
    # too repetitive
    if len(set(t.lower().split())) <= 5 and len(t) > 300:
        return True
    return False


# ============================================================
# EMBEDDINGS + VISION
# ============================================================

def get_local_embeddings():
    """
    Uses local Sentence Transformers via HuggingFace.
    Model: sentence-transformers/all-MiniLM-L6-v2 (384 dimensions)
    """
    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    model_kwargs = {'device': 'cpu'}
    encode_kwargs = {'normalize_embeddings': True}
    
    return HuggingFaceEmbeddings(
        model_name=model_name,
        model_kwargs=model_kwargs,
        encode_kwargs=encode_kwargs
    )


_VISION_CLIENT: Optional[ChatGoogleGenerativeAI] = None


def get_vision_client() -> ChatGoogleGenerativeAI:
    global _VISION_CLIENT
    if _VISION_CLIENT is None:
        _VISION_CLIENT = ChatGoogleGenerativeAI(
            model=VISION_MODEL_NAME,
            google_api_key=GOOGLE_API_KEY,
            temperature=0.1,
        )
    return _VISION_CLIENT


def encode_image(image: Image.Image) -> str:
    buf = io.BytesIO()
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")
    image.save(buf, format="JPEG", quality=90, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def generate_image_caption(image: Image.Image, prompt: str = "Analyze this image.") -> str:
    """
    Gemini Vision OCR / table extraction with basic retry/backoff.
    """
    chat = get_vision_client()
    img_base64 = encode_image(image)

    msg = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}},
        ]
    )

    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            resp = chat.invoke([msg])
            return (resp.content or "").strip()
        except Exception as e:
            last_err = e
            sleep_s = min(8, 2 ** attempt)
            logger.warning(f"Vision call failed (attempt {attempt}/3): {type(e).__name__}: {e}. Sleeping {sleep_s}s")
            time.sleep(sleep_s)

    logger.error(f"Vision failed after retries: {type(last_err).__name__}: {last_err}")
    return ""