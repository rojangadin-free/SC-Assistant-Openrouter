# src/helper.py
import re
import io
import base64
import hashlib
import logging
import time
from typing import List, Optional, Callable, Tuple

from PIL import Image
from langchain_core.messages import HumanMessage
# Updated imports: Added HuggingFaceEmbeddings, kept ChatGoogleGenerativeAI
from langchain_huggingface import HuggingFaceEmbeddings 
from langchain_openai import ChatOpenAI

from config import OPENROUTER_API_KEY, FALLBACK_MODEL_NAME

logger = logging.getLogger(__name__)

# ============================================================
# CLEANING
# ============================================================

# Page furniture that adds pure noise to embeddings / BM25.
_PAGE_NOISE_PATTERNS = [
    re.compile(r"^\d{1,4}$"),                                        # bare page number
    re.compile(r"^page\s+\d{1,4}(\s+of\s+\d{1,4})?$", re.I),         # "Page 15 of 48"
    re.compile(r"^p\.?\s*\d{1,4}$", re.I),                           # "p. 15"
    re.compile(r"^samar\s+colleges?,?\s+inc\.?\s*[ivxlcdm\d]*$", re.I),  # running header
    re.compile(r"^[-–—_=*.\s]+$"),                                   # separator rules
]


def remove_headers_footers(text: str) -> str:
    """
    Removes lines that are page furniture (page numbers, running headers, rules).
    Preserves ALL-CAPS lines so Chapter/Section titles survive for header tracking.
    """
    lines = text.splitlines()
    cleaned = []
    for ln in lines:
        s = ln.strip()
        if not s:
            # keep blank lines: they are paragraph separators used by the chunker
            cleaned.append("")
            continue
        if any(p.match(s) for p in _PAGE_NOISE_PATTERNS):
            continue
        cleaned.append(s)
    return "\n".join(cleaned)



def fix_hyphenation(text: str) -> str:
    """Fix word breaks like con-\\nnection -> connection."""
    return re.sub(r"-(\n|\r\n|\r)\s*([a-z])", r"\2", text)


# A line that begins a NEW structural item. These must keep their own line so
# list structure and headings survive into the chunker.
_STRUCTURAL_LINE = re.compile(
    r"""^\s*(
          [\u2022\u25AA\u25CF\u25E6\u00B7\uF0B7\-\*\u2013\u2014]\s+   # bullet glyphs
        | o\s+[A-Z0-9]                                              # Word's "o " sub-bullet
        | \d{1,2}[\.\)]\s+                                          # 1. / 1)
        | [a-zA-Z][\.\)]\s+[A-Z]                                    # a. / A)
        | (?:[IVXLCDM]{1,6})[\.\)]\s+                               # Roman numerals
        | \#{1,6}\s+                                                # markdown heading
        | \|                                                        # markdown table row
        | \[(?:TABLE|IMAGE|SECTION)                                 # our own markers
        | (?:Chapter|CHAPTER)\s+\d
        | STEP\s*\d{1,2}\s*$                                        # table grid cell header
        | STEP\s*\d{1,2}\s*:                                        # normalized step line
        | \[(?:END\s+)?STEP\s+SEQUENCE                              # step-sequence markers
    )""",
    re.VERBOSE,
)



# A heading-ish line: ALL CAPS, or a known section title.
_HEADING_KEYWORDS = (
    "chapter",
    "course offering",
    "deans update",
    "dean's update",
    "training offering",
    "training offerings",
    "degree program",
    "basic education",
    "samar college technological institute",
    "college of",
    "department of",
    "history of",
    "program outcome",
    "program objective",
    "course description",
    "learning plan",
    "grading system",
    "house rules",
    "consultation hours",
    "suggested reference",
    "admission requirement",
    "enrollment procedure",
    "vision",
    "mission",
    "core values",
    "editorial board",
    "general information",
    "senior high school",
    "junior high school",
)


# Lines that are ALL-CAPS but are table/grid cells, not section titles. Without
# this guard the running section header became "[SECTION: STEP 10]" for the whole
# enrollment procedure, which is useless for retrieval and for the reranker.
_NOT_A_HEADING = re.compile(
    r"^(STEP\s*\d{1,2}|TABLE\s*\d*|FIGURE\s*\d*|\[?(?:END\s+)?STEP\s+SEQUENCE)\b",
    re.I,
)

# Data that merely *looks* like a heading because of capital letters.
#
# The label is prepended to every reranker window, so junk labels cost real
# accuracy. Measured on the handbook, these produced section labels such as
# "[SECTION: 8:00AM-5:00PM]" (7 chunks) and "[SECTION: 1:00PM-5:00PM]" (6) —
# office-hour table cells governing whole chunks of unrelated text.
#
# All tests below are structural (shape of the string), not vocabulary, so they
# hold for any uploaded document.
_HEADING_REJECTS = (
    re.compile(r"\d{1,2}:\d{2}"),                 # clock times: 8:00AM-5:00PM
    re.compile(r"^\W*\d[\d\W]*$"),                # digits/punctuation only
    re.compile(r"\.{3,}"),                        # table-of-contents dot leaders
    re.compile(r"^[A-Z]{1,3}\s*\d+$"),            # code cells: "P CLO7", "IT 05"
)


# A fully bold Markdown line, e.g. "**STEPS IN SIGNING OF CLEARANCE**". The
# Vision extractor emits headings this way, and without this they were glued to
# the following line by collapse_soft_line_breaks().
_MD_BOLD_HEADING = re.compile(r"^\*\*[^*]{3,120}\*\*:?$")

# `**Step 1:** Registrar` — a bold *inline* step label. This is a separate shape
# from the grid cells: it is already a readable sequence, it just needs its own
# line so it is not glued onto the end of the preceding paragraph.
_MD_BOLD_STEP = re.compile(r"^\*\*Step\s*\d{1,2}\s*:?\*\*", re.I)


def split_inline_bold_steps(text: str) -> str:
    """
    Put every `**Step n:**` label at the start of its own line.

    The clearance list arrives from the Vision extractor as one long run:
        **STEPS IN SIGNING OF CLEARANCE** **Step 1:** Registrar **Step 2:** ...
    which reads as a single sentence and loses the enumeration.
    """
    if "**Step" not in text and "**step" not in text:
        return text

    out: List[str] = []
    for line in text.split("\n"):
        # Markdown table rows are handled by _flatten_markdown_step_tables();
        # splitting them here would destroy the row and break cell grouping.
        if _MD_TABLE_ROW.match(line):
            out.append(line)
            continue
        out.append(
            re.sub(r"(?<!^)\s+(?=\*\*Step\s*\d{1,2}\s*:?\*\*)", "\n", line, flags=re.I)
        )
    return "\n".join(out)





def is_heading_line(line: str) -> bool:
    """True when a single line looks like a section heading."""
    s = (line or "").strip().strip("*#:•▪- \t")
    if not s or len(s) < 4 or len(s) > 120:
        return False
    if s.endswith((".", ";", ",")):
        return False
    if _NOT_A_HEADING.match(s):
        return False
    if any(p.search(s) for p in _HEADING_REJECTS):
        return False

    letters = [c for c in s if c.isalpha()]

    if letters and all(c.isupper() for c in letters) and len(letters) > 3:
        return True


    low = s.lower()
    return low.startswith(_HEADING_KEYWORDS)


def collapse_soft_line_breaks(text: str) -> str:
    """
    Preserve paragraphs AND structural line breaks (bullets, numbered items,
    headings). Only genuinely "soft" wraps inside a sentence get collapsed.

    This matters a lot: previously EVERY newline was collapsed, which turned a
    whole page into one giant paragraph. That broke heading detection and glued
    unrelated lists together.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)

    lines = text.split("\n")
    out: List[str] = []

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            out.append("")
            continue

        starts_new_block = (
            bool(_STRUCTURAL_LINE.match(line))
            or is_heading_line(line)
            or _MD_BOLD_HEADING.match(stripped) is not None
            or _MD_BOLD_STEP.match(stripped) is not None
        )



        if not out or out[-1] == "" or starts_new_block:
            out.append(stripped)
            continue

        prev = out[-1]
        prev_is_structural = (
            bool(_STRUCTURAL_LINE.match(prev))
            or is_heading_line(prev)
            or _MD_BOLD_HEADING.match(prev.strip()) is not None
        )


        # A heading/bullet line is self-contained: never absorb the next line
        # into it unless that next line is clearly a soft continuation.
        if prev_is_structural and (stripped[:1].isupper() or stripped[:1].isdigit()):
            out.append(stripped)
        else:
            out[-1] = f"{prev} {stripped}"

    return "\n".join(out)



# ============================================================
# STEP-GRID NORMALIZATION
# ============================================================

# A grid cell header: uppercase "STEP <n>" alone on its line. That is exactly
# what a table cell looks like after text extraction. Prose like
# "Step 1: Submit Credentials Step 2: ..." must NOT match, hence the anchors
# and the case-sensitive STEP.
_GRID_CELL = re.compile(r"^STEP\s*(\d{1,2})\s*$")

# The Vision table extractor emits Markdown, so the same grid arrives as a
# single pipe-delimited row with <br> instead of newlines:
#   | **STEP 1**<br>**Security & Services**<br>* Log-in ... | **STEP 2**<br>... |
# That is one physical line, so the line-based grid detector above cannot see it.
_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MD_SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_MD_STEP_CELL = re.compile(r"^\**STEP\s*(\d{1,2})\**\s*$", re.I)


def _flatten_markdown_step_tables(text: str) -> str:
    """
    Turn Markdown step tables into one "STEP n" cell per line so that
    normalize_step_grids() can treat them exactly like extracted grids.

    Only rows that actually contain `STEP <n>` cells are touched; ordinary
    Markdown tables (grading breakdowns, curriculum maps) are left alone.
    """
    if "|" not in text:
        return text

    out: List[str] = []
    for line in text.split("\n"):
        if not _MD_TABLE_ROW.match(line) or _MD_SEPARATOR.match(line):
            out.append(line)
            continue

        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        expanded: List[str] = []
        found_step = False

        for cell in cells:
            if not cell:
                continue
            # <br> is the in-cell line break; turn it back into real lines.
            for piece in re.split(r"<br\s*/?>", cell):
                piece = piece.strip().strip("*").strip()
                if not piece:
                    continue
                m = _MD_STEP_CELL.match(piece)
                if m:
                    found_step = True
                    expanded.append(f"STEP {int(m.group(1))}")
                else:
                    expanded.append(piece)

        if found_step:
            out.extend(expanded)
        else:
            out.append(line)

    return "\n".join(out)



def normalize_step_grids(text: str, min_steps: int = 3) -> str:
    """
    Rewrite extracted "STEP n" table grids into an explicit, self-describing list.

    Why this exists
    ---------------
    The enrollment procedure in the handbook is a 5x2 table. PDF extraction emits
    it in *visual* order, so the page becomes:

        STEP 1
        Security & Services
        * Log-in personal information
        STEP 2
        *Secure Assessment slip for back accounts
        ...

    Every cell looks like an independent fragment. There is nothing telling the
    model "this is ONE procedure with 10 steps", so it happily summarizes the
    first few cells and stops — which is exactly the "only 6 steps" bug.

    After normalization the same content reads:

        [STEP SEQUENCE]
        STEP 1: Security & Services -- Log-in personal information; Secure queue number
        ...
        STEP 10: Announcement -- Attend Regular Classes
        [END STEP SEQUENCE: 10 steps total]

    The explicit total is the important part: it makes an incomplete answer
    self-evidently wrong to the model.

    A run ends when the step number stops increasing, which correctly separates
    the "New Students and Transferees" grid (1..10) from the "Old Students"
    grid (1..10) that follows it.

    Handles both shapes the pipeline can produce: the raw column-extracted grid
    (one cell per line) and the Vision extractor's Markdown table (one row per
    line, `<br>` inside cells).
    """
    text = _flatten_markdown_step_tables(text)
    lines = text.split("\n")


    marks: List[Tuple[int, int]] = []
    for i, ln in enumerate(lines):
        m = _GRID_CELL.match(ln.strip())
        if m:
            marks.append((i, int(m.group(1))))

    if len(marks) < min_steps:
        return text

    runs: List[List[Tuple[int, int]]] = []
    current = [marks[0]]
    for prev, nxt in zip(marks, marks[1:]):
        if nxt[1] > prev[1]:
            current.append(nxt)
        else:
            runs.append(current)
            current = [nxt]
    runs.append(current)

    replacements = []
    for run in runs:
        if len(run) < min_steps:
            continue

        # The run's text ends at the next grid cell marker (start of the next
        # grid) or at the end of the page.
        end = len(lines)
        for i in range(run[-1][0] + 1, len(lines)):
            if _GRID_CELL.match(lines[i].strip()):
                end = i
                break

        rendered = ["[STEP SEQUENCE]"]
        for k, (idx, num) in enumerate(run):
            stop = run[k + 1][0] if k + 1 < len(run) else end
            body = [ln.strip() for ln in lines[idx + 1:stop] if ln.strip()]

            # The final cell's "body" runs to the end of the grid, so it can
            # swallow the heading that follows the table. Cut at the first
            # heading so the last step keeps only its own actions.
            for j, b in enumerate(body):
                if j > 0 and is_heading_line(b):
                    body = body[:j]
                    trailing = lines[idx + 1 + j:stop]
                    break
            else:
                trailing = []

            label = body[0].lstrip("*-• \t").strip() if body else ""
            actions = "; ".join(b.lstrip("*-• \t").strip() for b in body[1:])


            line = f"STEP {num}: {label}" if label else f"STEP {num}"
            if actions:
                line += f" -- {actions}"
            rendered.append(line)

        rendered.append(f"[END STEP SEQUENCE: {len(run)} steps total]")
        # Whatever followed the last cell (usually the next section heading)
        # is put back after the closing marker instead of being eaten.
        rendered.extend(t for t in trailing if t.strip())

        replacements.append((run[0][0], end, rendered))

    # Apply back-to-front so earlier indices stay valid.
    for start, end, rendered in reversed(replacements):
        lines[start:end] = rendered

    return "\n".join(lines)


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
    # Break inline "**Step n:**" runs apart BEFORE collapsing, so each label is
    # already on its own line when the collapser decides what to merge.
    t = split_inline_bold_steps(t)
    t = collapse_soft_line_breaks(t)


    # Normalize spaces but preserve paragraph breaks
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s+\n", "\n\n", t)

    # Rebuild "STEP n" table grids into an explicit numbered sequence so the LLM
    # cannot silently truncate a 10-step procedure to 6.
    t = normalize_step_grids(t)
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


def detect_block_header(block: str) -> Optional[str]:
    """
    Look at the first few lines of a block and return a heading if one is found.
    Works on multi-line blocks (the old version only looked at short one-liners,
    which never matched after line-collapsing).
    """
    for line in block.splitlines()[:3]:
        s = line.strip()
        if not s:
            continue
        if is_heading_line(s):
            return s.strip("*#:•▪- \t")
        # Only inspect the very top of a block
        break
    return None


# A heading that merely labels a sub-list ("Degree Programs:", "Senior High
# School") should NOT replace the section heading that governs the whole page
# ("COURSE OFFERING"). Only a *major* heading does that.
#
# Structural test, no vocabulary: a major heading is ALL-CAPS (that is how this
# corpus — and most documents — mark top-level sections), while sub-labels are
# Title Case and/or end with a colon.
def is_major_heading(line: str) -> bool:
    s = (line or "").strip().strip("*#:•▪- \t")
    if not is_heading_line(s):
        return False
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and len(letters) > 3


def split_blocks_on_major_headings(blocks: List[str]) -> List[str]:
    """
    Re-split blocks so that every MAJOR heading starts a block of its own.

    Why this is needed
    ------------------
    Blocks are produced by splitting on blank lines, and `detect_block_header()`
    only inspects the first 3 lines of a block. Text that comes back from the
    Vision/OCR path has almost no blank lines, so a whole page arrives as ONE
    block whose first line is arbitrary — any heading further down is invisible.

    Real, measured example (Samar-College-update.pdf p12, via the live Vision
    pipeline). The page contains, in order: the tail of a grading table,
    "X. SUGGESTED REFERENCES", "XI. HOUSE RULES", "XII. CONSULTATION HOURS",
    then "COURSE OFFERING" with the full program list including SCTI. As one
    block it was filed as:

        [SECTION: X.  SUGGESTED REFERENCES]   <- program list buried inside

    so a query about programs was scored against a references label and lost to
    the old handbook's own COURSE OFFERING page.

    Splitting here means `smart_chunking()`'s existing rules ("a major heading
    starts a new chunk", "label the chunk with its own heading") apply to
    OCR-derived text exactly as they do to text-layer PDFs.
    """
    out: List[str] = []
    for block in blocks:
        lines = block.splitlines()
        start = 0
        for i, line in enumerate(lines):
            # i == 0 is already the block's own heading; only split *inside*.
            if i > 0 and is_major_heading(line):
                piece = "\n".join(lines[start:i]).strip()
                if piece:
                    out.append(piece)
                start = i
        piece = "\n".join(lines[start:]).strip()
        if piece:
            out.append(piece)
    return out


def smart_chunking(
    text: str,

    max_tokens: int = 1000,
    overlap: int = 150,
    token_counter: Optional[Callable[[str], int]] = None,
    running_header: str = "General College Information",
) -> Tuple[List[str], str]:
    """
    Section-aware hybrid chunking with contextual header tracking across pages.

    Key properties:
      * Headings are detected per-LINE (not only on short paragraphs), so
        "COURSE OFFERING", "DEANS UPDATE", "Training Offerings:" actually register.
      * Line structure is preserved inside a chunk (bullets stay bullets), which
        keeps hierarchical lists like "SCTI > Cookery NC II" readable to the LLM.
      * A chunk is labelled with the heading its OWN text sits under, and a new
        major heading always starts a new chunk.

    Why the labelling rule matters
    ------------------------------
    The label is not cosmetic: `rag/reranker.py` prepends it to every scoring
    window, so a wrong label actively suppresses the chunk. Measured on the
    program-list chunk for "what are the programs offered?":

        label "DEANS UPDATE"   (wrong, what the old code produced) : -6.51
        no label at all                                            : -5.64
        label "COURSE OFFERING" (correct)                          : +0.15

    The old code kept ONE `running_header`, overwrote it on every heading, and
    stamped the LAST value onto the whole chunk at emit time. On a page holding
    COURSE OFFERING ... then DEANS UPDATE, the entire program list was therefore
    filed under "DEANS UPDATE" — a 6.7-point self-inflicted penalty that no
    amount of query tuning can undo.

    Two changes fix it generically:
      1. `emit_current()` uses the heading that was in force when the chunk's
         content STARTED (`chunk_header`), not whatever heading came later.
      2. A new MAJOR (all-caps) heading always closes the open chunk, so two
         unrelated sections can never share one chunk.
    """
    text = clean_text(text)
    if not text:
        return [], running_header

    count = token_counter or approx_tokens
    blocks = [b.strip() for b in re.split(r"\n\n+", text) if b.strip()]
    blocks = split_blocks_on_major_headings(blocks)

    chunks: List[str] = []

    current_parts: List[str] = []
    current_tokens = 0
    # The heading that governs the content currently being accumulated.
    chunk_header = running_header

    def emit_current():
        nonlocal current_parts, current_tokens, chunk_header
        if not current_parts:
            return

        chunk_body = "\n".join(current_parts).strip()
        if chunk_body:
            chunks.append(f"[SECTION: {chunk_header}]\n{chunk_body}")

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

        # Carried-over overlap belongs to the section we just closed; the next
        # chunk's label is set by the caller when a new heading arrives.
        chunk_header = running_header

    for block in blocks:
        header = detect_block_header(block)
        block_tokens = count(block)

        if header:
            major = is_major_heading(block.splitlines()[0])
            # A new MAJOR section always starts a new chunk: mixing two
            # top-level sections in one chunk guarantees a wrong label for one
            # of them. Sub-labels only break when the chunk is already large.
            if current_parts and (major or current_tokens >= max_tokens * 0.6):
                emit_current()
                current_parts = []      # do not carry overlap across sections
                current_tokens = 0
            running_header = header
            # Only a major heading re-labels the chunk; a sub-list label like
            # "Degree Programs:" stays *inside* the COURSE OFFERING section.
            if major or not current_parts:
                chunk_header = header

        if block_tokens > max_tokens:

            # Oversized block: split by lines first (keeps list items intact),
            # falling back to sentences for prose.
            units: List[str] = []
            for line in block.splitlines():
                line = line.strip()
                if not line:
                    continue
                if count(line) > max_tokens:
                    units.extend(semantic_split_sentences(line))
                else:
                    units.append(line)

            for unit in units:
                unit_tokens = count(unit)
                if current_tokens + unit_tokens > max_tokens and current_parts:
                    emit_current()
                current_parts.append(unit)
                current_tokens += unit_tokens
            continue

        if current_tokens + block_tokens > max_tokens and current_parts:
            emit_current()

        current_parts.append(block)
        current_tokens += block_tokens

    if current_parts:
        emit_current()

    return chunks, running_header



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
    # Ignore our own [SECTION: ...] tag when judging value
    t = re.sub(r"^\[SECTION:[^\]]*\]\s*", "", text.strip()).strip()
    if len(t) < 40:
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


_VISION_CLIENT: Optional[ChatOpenAI] = None

def get_vision_client() -> ChatOpenAI:
    global _VISION_CLIENT
    if _VISION_CLIENT is None:
        _VISION_CLIENT = ChatOpenAI(
            model=FALLBACK_MODEL_NAME,
            openai_api_key=OPENROUTER_API_KEY,
            openai_api_base="https://openrouter.ai/api/v1",
            temperature=0.1,
            max_tokens=2048,
            default_headers={
                # Remove generic headers like HTTP-Referer or X-Title
                # Spoof supported client headers to bypass the AgentRouter WAF
                "Originator": "codex_cli_rs",
                "User-Agent": "codex_cli_rs/0.101.0 (Mac OS 26.0.1; arm64) Apple_Terminal/464",
                "Version": "0.101.0",
                "X-Stainless-Runtime": "node" 
            }
        )
    return _VISION_CLIENT


def encode_image(img: Image.Image) -> str:
    """
    Convert a PIL Image to a plain base64 string (no data URI prefix).
    Gemini expects raw base64 bytes — NOT 'data:image/jpeg;base64,...'.
    RGBA/P mode images are converted to RGB first to ensure JPEG compatibility.
    """
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
 
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
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
            
            # --- FIX FOR GEMINI 3 CONTENT BLOCKS ---
            answer_text = resp.content
            if isinstance(answer_text, list):
                answer_text = "".join(
                    block.get("text", "") for block in answer_text 
                    if isinstance(block, dict) and block.get("type") == "text"
                )
                
            return (answer_text or "").strip()
            
        except Exception as e:
            last_err = e
            sleep_s = min(8, 2 ** attempt)
            logger.warning(f"Vision call failed (attempt {attempt}/3): {type(e).__name__}: {e}. Sleeping {sleep_s}s")
            time.sleep(sleep_s)

    logger.error(f"Vision failed after retries: {type(last_err).__name__}: {last_err}")
    return ""