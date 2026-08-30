"""
rag/language.py — make Tagalog and Waray questions retrieve English documents.

The problem
-----------
Every document in this system is written in English. Every student in Samar asks
in a mix of English, Tagalog and Waray-Waray:

    "Pila an bayad sa enrollment?"          (Waray)
    "Magkano ang tuition fee?"              (Tagalog)
    "Sin-o an dean han education?"          (Waray)
    "Kailan ang deadline ng payment?"       (Taglish)

Retrieval is embedding + BM25 over English text. BM25 scores a Waray word at
exactly zero because the token never appears in any document, and the embedding
model — a small English MiniLM — puts "pila an bayad" nowhere near "tuition
fees". So the question that a Samar student is *most likely* to type is the one
the system handles worst. The answer is not wrong; it never finds the page.

The fix, and why it is a dictionary
----------------------------------
Ask an LLM to translate and every question costs an extra round-trip before
retrieval can even start — on the request path, for the one population who
already has the worst experience. Machine translation also fails in the specific
way that matters here: "Waray" is both the language and the word for "none", and
translators regularly render *"pila"* as "several" (Tagalog) instead of "how
much" (Waray).

So the mapping is an explicit table of the words that actually appear in campus
questions. It is smaller than a translator, it is deterministic, it is inspectable
by the staff who will maintain it, and it costs nothing per request.

What it does NOT do
-------------------
It does not translate sentences and it does not touch the question the student
sees or the language of the answer — that is the prompt's job. This module only
*adds* an English retrieval variant so the search terms exist in the index at
all. The student's original wording is always kept and always ranked, so a
question this table does not recognise is no worse off than before.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# The table.
#
# Keys are the Tagalog / Waray / Taglish forms students type; values are the
# English words that appear in the documents. Multi-word keys are matched first,
# because "magkano ang bayad" should become "how much fee", not "how much" plus a
# separately-translated "bayad".
#
# Deliberately campus vocabulary only. This is not a dictionary of two languages;
# it is the vocabulary of registrar, cashier and enrollment questions, which is
# what the documents are about.
#
# No identity entries. An earlier draft mapped words that are ALREADY English —
# "tuition" -> "tuition", "dean" -> "dean", "requirements" -> "requirements" —
# which looked harmless and was not: a plain English question containing any of
# them ("Who is the dean?") counted as a "local" hit and got rewritten. The
# rewrite added nothing but did fire the whole code path, so a working question
# started paying for an extra Pinecone round-trip and a duplicate ranking slot.
# A key belongs here only when it is a word the DOCUMENTS DO NOT CONTAIN.
# ---------------------------------------------------------------------------


_TERMS: Dict[str, str] = {
    # ---- question words -------------------------------------------------
    # "pila" is the single highest-value entry in this table: it opens most
    # Waray fee questions and means "how much", which no English-trained
    # embedding will ever guess from the token.
    "pila": "how much",
    "pila an": "how much is",
    "magkano": "how much",
    "sin-o": "who",
    "sino": "who",
    "kanay": "whose",
    "kailan": "when",
    "kanus-a": "when",
    "san-o": "when",
    "saan": "where",
    "hain": "where",
    "diin": "where",
    "paano": "how",
    "paunan-o": "how",
    "ano": "what",
    "anong": "what",
    "nano": "what",
    "bakit": "why",
    "kay ano": "why",
    "alin": "which",

    # ---- money ----------------------------------------------------------
    "bayad": "payment fee",
    "bayaran": "payment fee",
    "babayaran": "payment fee",
    "bayarin": "fees",
    "matrikula": "tuition",

    "kabayaran": "payment",
    "gastos": "cost expenses",
    "presyo": "price cost",
    "multa": "penalty fine",
    "utang": "balance owed",
    "balanse": "balance",
    "hulog": "installment",
    "hulugan": "installment",
    "diskwento": "discount",
    "iskolarship": "scholarship",
    "eskolar": "scholar scholarship",

    # ---- enrollment -----------------------------------------------------
    "enrol": "enroll enrollment",
    "enrolment": "enrollment",
    "magpaenroll": "enroll enrollment",
    "magparehistro": "register registration",
    "rehistro": "registration",
    "pasok": "admission entry",
    "pumasok": "enter admission",
    "lipat": "transfer",
    "balik": "returning",
    "kinahanglan": "requirements needed",
    "kailangan": "requirements needed",
    "papeles": "documents papers requirements",
    "dokumento": "documents",
    "porma": "form",


    # ---- academics ------------------------------------------------------
    "kurso": "course program",
    "kursong": "course program",
    "programa": "program",
    "asignatura": "subject",
    "klase": "class",
    "titser": "teacher instructor faculty",
    "maestro": "teacher instructor",
    "maestra": "teacher instructor",
    "propesor": "professor faculty",
    "dekano": "dean",
    "prinsipal": "principal",

    "punong-guro": "principal",
    "eskwelahan": "school",
    "eskuylahan": "school",
    "tunghaan": "school",
    "kolehiyo": "college",
    "unibersidad": "university",
    "departamento": "department",
    "opisina": "office",
    "kwarto": "room",
    "grado": "grade grades",
    "marka": "grade grades",
    "resulta": "result results",
    "eksam": "exam examination",
    "eksamen": "exam examination",
    "pasulit": "exam quiz",
    "pagsulit": "exam quiz",
    "proyekto": "project",
    "takdang-aralin": "assignment homework",

    # ---- time -----------------------------------------------------------
    "petsa": "date",
    "oras": "time hours",
    "takdang oras": "schedule time",
    "iskedyul": "schedule",
    "eskedyul": "schedule",
    "semestre": "semester",
    "pasukan": "school year opening classes",

    "bakasyon": "vacation break holiday",
    "piyesta": "holiday",
    "bukas": "tomorrow",
    "buwas": "tomorrow",
    "ngayon": "today",
    "yana": "today",
    "kanina": "earlier",
    "huli": "late",
    "hangtod": "until",

    "hanggang": "until",

    # ---- people & places ------------------------------------------------
    "estudyante": "student",
    "eskolar": "student scholar",
    "magulang": "parent guardian",
    "kabataan": "youth student",
    "librarya": "library",
    "aklatan": "library",
    "kantina": "canteen cafeteria",
    "dormitoryo": "dormitory",
    "klinika": "clinic",

    # ---- common verbs / asks --------------------------------------------
    "pwede": "can allowed",
    "puwede": "can allowed",
    "mahimo": "can allowed",
    "bawal": "not allowed prohibited",
    "dili": "not",
    "hindi": "not",
    "wala": "none no",
    "waray": "none no",
    "may": "is there",
    "meron": "is there",
    "mayda": "is there",
    "makakuha": "get obtain",
    "kuha": "get obtain",
    "hatag": "give",
    "ihatag": "give",
    "sabi": "said states",
    "tudlo": "teach",
    "tabang": "help assistance",
    "tulong": "help assistance",
    "kopya": "copy",
    "sertipiko": "certificate",
    "katibayan": "certificate proof",
    "tala": "records",
    "rekord": "records",
}

# Politeness and glue words. They carry no retrieval signal, and leaving them in
# the English variant costs BM25 accuracy for no gain.
_PARTICLES = {
    "po", "ba", "naman", "nga", "man", "gud", "la", "na", "pa", "ka", "ko",
    "ako", "ikaw", "siya", "kami", "kita", "kamo", "sila", "ako'y",
    "ang", "an", "sa", "han", "hin", "ng", "nga", "si", "ni", "kay", "para",
    "yung", "iyong", "ito", "iyan", "adto", "ini", "nga", "daw", "raw",
    "kuya", "ate", "sir", "maam", "ma'am", "mam",
}

# A question needs at least this many recognised non-English words before it is
# treated as Tagalog/Waray. One word is usually an English question containing a
# borrowed term ("what is the deadline") and rewriting it adds noise.
MIN_LOCAL_HITS = 1

_WORD_RE = re.compile(r"[A-Za-z\u00f1\u00d1][A-Za-z0-9\u00f1\u00d1\-']*")

# Longest keys first so multi-word phrases win over their own components.
_SORTED_KEYS: Tuple[str, ...] = tuple(
    sorted(_TERMS, key=lambda k: (-len(k.split()), -len(k)))
)


def _norm(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def detect_local_terms(question: str) -> List[str]:
    """
    The recognised Tagalog/Waray words in `question`, in the order they appear.

    Used both to decide whether to translate at all and to explain the decision
    in tests and logs — a silent rewrite is very hard to debug when retrieval
    later goes strange.
    """
    text = _norm(question)
    if not text:
        return []

    found: List[str] = []
    remaining = f" {text} "

    for key in _SORTED_KEYS:
        needle = f" {key} "
        if needle in remaining:
            found.append(key)
            # Consume the match so "pila an" does not also report "pila".
            remaining = remaining.replace(needle, " ")

    return found


def is_local_language(question: str) -> bool:
    """True when the question carries enough local vocabulary to be worth a
    rewrite. English questions must answer False: rewriting them would only add
    duplicate search terms."""
    return len(detect_local_terms(question)) >= MIN_LOCAL_HITS


def to_english_query(question: str) -> str:
    """
    An English *search string* for a Tagalog/Waray question. Not a translation —
    word order and grammar are irrelevant to BM25 and near-irrelevant to a
    bag-of-subwords embedding, so no effort is spent on them.

    Returns "" when nothing was recognised, so callers can cheaply skip.
    """
    text = _norm(question)
    if not text:
        return ""

    padded = f" {text} "
    hits = 0

    for key in _SORTED_KEYS:
        needle = f" {key} "
        if needle in padded:
            padded = padded.replace(needle, f" {_TERMS[key]} ")
            hits += 1

    if hits < MIN_LOCAL_HITS:
        return ""

    # Drop particles and de-duplicate while preserving order. Duplicates arise
    # naturally ("bayad" and "bayaran" both yield "payment fee") and a repeated
    # term inflates its own BM25 weight for no reason.
    seen = set()
    out: List[str] = []
    for w in padded.split():
        if w in _PARTICLES or w in seen:
            continue
        seen.add(w)
        out.append(w)

    return " ".join(out)


def language_variants(question: str) -> List[str]:
    """
    Extra retrieval queries for `question` — [] for an English one.

    The caller keeps the student's original wording as the primary query. This
    only adds, never replaces: a mistranslation should cost a wasted candidate
    slot, never the question the student actually asked.
    """
    english = to_english_query(question)
    if not english:
        return []

    variants = [english]

    # Thin rewrites match a fee TABLE better when the enumerative phrasing is
    # also tried — the same trick expand_queries() uses for thin English
    # questions. The threshold counts words AFTER expansion, and expansion
    # inflates the count: "Pila an bayad?" (two words, as thin as a question
    # gets) becomes the FIVE-word "how much is payment fee", so a <= 4 cutoff
    # skipped exactly the queries it was written for. It is measured on the
    # student's own question instead.
    if len(question.split()) <= 4:
        variants.append(f"{english} list of fees and amounts")


    return variants
