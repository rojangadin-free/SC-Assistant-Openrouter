import os
import re
import concurrent.futures  # 🚀 Added for parallel processing
from datetime import datetime

from langchain_openai import ChatOpenAI
from config import OPENROUTER_API_KEY, CHAT_MODEL_NAME
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict, List, Dict, Optional
from pinecone import Pinecone

from langchain_community.retrievers import PineconeHybridSearchRetriever
from langchain_classic.retrievers import EnsembleRetriever
from pinecone_text.sparse import BM25Encoder

from src.helper import get_local_embeddings
from src.prompt import system_prompt
from rag.reranker import rerank, rerank_multi
from rag.conflicts import authority_block
from rag.freshness import freshness_block, source_priority, doc_key

from rag.citations import build_citations

from rag.calendar import calendar_block
from rag.announcements import announcement_block
from rag.roles import resolve_role, announcement_audience, role_block

from rag.language import language_variants
from rag.dictation import dictation_variants
from rag.latency import (
    should_optimize, pick_search_query, Deadline, Timings,
)
# The captions the student sees while waiting. Published from the real phase
# boundaries below rather than on a timer in the browser, so "Searching…" is
# shown exactly while Pinecone is being searched. See rag/progress.py.
from rag.progress import emit as emit_phase









from config import (
    INDEX_NAME, CHAT_MODEL_NAME, FALLBACK_MODEL_NAME, SUMMARIZER_MODEL_NAME,
    PINECONE_API_KEY, OPENROUTER_API_KEY, AGENTROUTER_API_KEY
)

# Student records
from aws.students import get_student_by_uid, get_student_by_email, format_student_context

# ====== Setup ======
embeddings = get_local_embeddings()
pinecone = Pinecone(api_key=PINECONE_API_KEY)
index = pinecone.Index(INDEX_NAME)

# --- Retrievers ---
bm25_path = "bm25_values.json"
if os.path.exists(bm25_path):
    bm25 = BM25Encoder().load(bm25_path)
else:
    print("WARNING: bm25_values.json not found. Using default BM25.")
    bm25 = BM25Encoder().default()

# Deeper candidate pools: institutional docs repeat the same vocabulary on many
# pages, so a shallow pool silently drops the one page that actually answers.
RETRIEVER_TOP_K = 15

sparse_retriever = PineconeHybridSearchRetriever(
    embeddings=embeddings,
    sparse_encoder=bm25,
    index=index,
    top_k=RETRIEVER_TOP_K,
    alpha=0.0,
)

dense_retriever = PineconeHybridSearchRetriever(
    embeddings=embeddings,
    sparse_encoder=bm25,
    index=index,
    top_k=RETRIEVER_TOP_K,
    alpha=1.0,
)

# Rebalanced toward sparse: rare exact tokens (acronyms, codes, proper nouns)
# are precisely where BM25 wins and dense embeddings blur.

retriever = EnsembleRetriever(
    retrievers=[sparse_retriever, dense_retriever],
    weights=[0.45, 0.55],
)

# The cross-encoder reorders the pool, so we can afford to keep the pool wide
# (recall) while sending only the genuinely relevant docs to the LLM (precision).
RERANK_CANDIDATES = 25  # how many candidates the cross-encoder scores
FINAL_TOP_K = 12        # documents actually sent to the LLM


# ---------------------------------------------------------------------------
# Query expansion — document-agnostic.
#
# An earlier version of this file hardcoded the answer ("SCTI ... Cookery NC II
# ... Pre-school") as a companion query. That made ONE question on ONE PDF work
# and taught the system nothing: any newly uploaded document was still broken.
#
# What actually goes wrong with a short question is measurable. Cross-encoder
# scores for the chunk that holds the complete program list:
#
#   "what are the programs offered?"            ->  0.17   (ranked 6th)
#   "programs offered"                          ->  2.61   (ranked 3rd)
#   "list of programs offered by Samar College" ->  8.81   (ranked 1st)
#
# Same chunk, same index, same model. A 4-word question simply does not carry
# enough signal for either BM25 or a cross-encoder. So instead of injecting the
# expected answer, we generate *generic* paraphrases that add the structural
# words a list-shaped answer contains ("list of", "complete", "all") and let the
# reranker decide. Nothing here mentions any specific school, course or heading.
# ---------------------------------------------------------------------------

# Words that carry no retrieval signal — used to measure how "thin" a query is.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "am", "do",
    "does", "did", "what", "which", "who", "whom", "whose", "when", "where",
    "why", "how", "and", "or", "but", "if", "then", "than", "of", "in", "on",
    "at", "to", "for", "with", "by", "from", "about", "as", "that", "this",
    "these", "those", "there", "here", "it", "its", "can", "could", "should",
    "would", "will", "shall", "may", "might", "must", "have", "has", "had",
    "you", "your", "me", "my", "i", "we", "our", "us", "they", "them", "their",
    "please", "tell", "give", "show", "list", "all", "any", "some", "also",
}

# A query with fewer than this many content words is treated as under-specified
# and gets paraphrased.
THIN_QUERY_TERMS = 4


def _content_terms(query: str) -> List[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-']+", (query or "").lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def expand_queries(query: str) -> List[str]:
    """
    Return [main_query] plus generic paraphrases for thin queries.

    The paraphrases are built purely from the user's own content words, so this
    works identically for a school handbook, a lease agreement or a lab manual.
    """
    query = (query or "").strip()
    if not query:
        return []

    queries = [query]

    # Dictation repair BEFORE anything else looks at the words, because the
    # artefacts it removes are invisible to every step below. A recogniser hands
    # back "what is b s i t": `_content_terms()` sees four one-character tokens,
    # discards all of them, and the thin-query branch below then has nothing to
    # build a paraphrase from — while BM25 scores the letters at zero because
    # they appear in no document. The repaired copy ("what is bsit") is the one
    # that carries a term the index has actually seen.
    #
    # Additive, like the language variants: the student's exact words stay first
    # and are still ranked, so a false positive costs one extra probe and can
    # never replace what they said. See rag/dictation.py.
    spoken = ""
    for variant in dictation_variants(query):
        if variant not in queries:
            queries.append(variant)
            spoken = spoken or variant

    # Tagalog/Waray first, because the terms it produces are what the rest of

    # this function has to work with. "Pila an bayad?" carries no English content
    # word at all: BM25 scores it zero (the tokens are in no document) and the
    # English MiniLM puts it nowhere near "tuition fees". Without this the
    # question a Samar student is most likely to type is the one retrieval
    # handles worst. See rag/language.py.
    #
    # Additive on purpose — the student's own wording stays first and is still
    # ranked, so a term the table does not know costs nothing.
    english = ""
    for variant in language_variants(query):
        if variant not in queries:
            queries.append(variant)
            english = english or variant

    # Thinness is judged on whichever string actually carries English terms. A
    # local-language question has ~0 recognisable content words, so measuring the
    # original would call every one of them thin and bolt "complete list of" onto
    # a query that is already a rewrite. A dictated one is worse than that: every
    # letter of "b s i t" is dropped as too short, so the original measures as
    # having NO terms at all and the paraphrase branch below has nothing to build
    # from. Both cases want the repaired/translated string, not what arrived.
    terms = _content_terms(english or spoken or query)


    # Thin query -> add reformulations that bias toward enumerative passages.
    if len(terms) < THIN_QUERY_TERMS and terms:
        core = " ".join(terms)
        for extra in (f"complete list of {core}", f"{core} include the following"):
            if extra not in queries:
                queries.append(extra)

    return queries




# ---------------------------------------------------------------------------
# Multi-part question splitting — document-agnostic.
#
# "who are the principal of highschool, elementary and who is the dean of
# citas?" is three questions in one sentence. Ranking it as a single query makes
# the final list majority-rule: the two principal asks dominate, and the CITAS
# evidence lands at rank 13 of a 12-slot list — retrieved, then thrown away.
#
# So the question is split on its coordinators (and / or / commas / semicolons)
# and each part is ranked separately. Splitting is purely syntactic, so it works
# on any document set and any subject matter.
# ---------------------------------------------------------------------------

# Interrogatives that mark the start of a new ask ("... and WHO is the dean").
_QUESTION_WORDS = (
    "who", "what", "when", "where", "why", "how", "which", "whose", "whom",
)

# A fragment that already names its own thing ("the dean of citas") begins with
# one of these. It needs an interrogative, NOT the previous part's noun.
_DETERMINERS = ("the", "a", "an", "its", "their", "his", "her")

# Splitting a 6-word question into fragments produces noise, not aspects.
MIN_PART_TERMS = 1
MAX_PARTS = 4


def split_question(question: str) -> List[str]:
    """
    Split a compound question into its parts. Returns [] when the question is
    single-purpose (the common case), so callers can skip the extra work.

    The split is conservative: a part is kept only if it still carries a content
    word, and the original question is always ranked alongside the parts, so an
    over-eager split cannot lose information.

    Two kinds of coordinated fragment, and they need opposite treatment
    -------------------------------------------------------------------
    1. The fragment shares the first part's noun:
         "who are the principal of highschool, elementary and ..."
         "elementary" means "the principal of elementary"
       -> prepend the first part's stem, up to and including its preposition:
          "who are the principal of" + "elementary"

    2. The fragment already names its own thing:
         "who is the president of samar college and the dean of citas"
         "the dean of citas" is complete; it only lacks the interrogative.
       -> prepend just the question word: "who is" + "the dean of citas"

    Getting this backwards is what broke the president/CITAS/principal question.
    The stem was applied in case 2, producing:

        "who is the president of samar the dean of citas"
        "who is the president of samar the principal of highschool"

    Those describe no role in any document, so the cross-encoder fell back on the
    only words it recognised — "president of samar" — and all three aspects
    ranked the same HISTORICAL ACCOUNT pages. The answer named the president and
    guessed at the other two. A fragment starting with a determiner is therefore
    treated as self-contained.
    """
    q = (question or "").strip()
    if not q:
        return []

    # Split on commas, semicolons, and the words "and"/"or" used as coordinators.
    raw_parts = re.split(r"\s*[;,]\s*|\s+\band\b\s+|\s+\bor\b\s+", q, flags=re.I)
    parts = [p.strip(" ?.!") for p in raw_parts if p and p.strip(" ?.!")]

    if len(parts) < 2:
        return []

    first_words = parts[0].split()
    lead_is_question = bool(first_words) and \
        first_words[0].strip("?,.").lower() in _QUESTION_WORDS

    # "who is" / "what are" — the minimal interrogative opener, for case 2.
    opener = ""
    # "who are the principal of" — the full stem, for case 1.
    stem = ""

    if lead_is_question:
        opener = " ".join(first_words[:2]) if len(first_words) > 1 else first_words[0]

        # The stem is everything before the first part's own head noun, so the
        # fragment can be substituted in its place. Cut after a preposition
        # when there is one ("... principal OF highschool"), because that is
        # exactly the slot a bare fragment fills.
        lowered = [w.strip("?,.").lower() for w in first_words]
        cut = None
        for i, w in enumerate(lowered):
            if w in ("of", "for", "in", "at"):
                cut = i + 1
        if cut is not None and cut < len(first_words):
            stem = " ".join(first_words[:cut]).strip()

    aspects: List[str] = []
    for i, p in enumerate(parts):
        if len(_content_terms(p)) < MIN_PART_TERMS:
            continue

        words = p.split()
        first_word = words[0].strip("?,.").lower() if words else ""

        if i == 0 or first_word in _QUESTION_WORDS:
            aspect = p                                  # already a question
        elif first_word in _DETERMINERS and opener:
            aspect = f"{opener} {p}"                    # case 2: self-contained
        elif stem:
            aspect = f"{stem} {p}"                      # case 1: shares the noun
        elif opener:
            aspect = f"{opener} {p}"
        else:
            aspect = p

        aspect = aspect.strip()
        if aspect and aspect not in aspects:
            aspects.append(aspect)

    if len(aspects) < 2:
        return []

    return aspects[:MAX_PARTS]



def multi_query_retrieve(query: str, extra_queries: List[str] = None) -> list:

    """
    Fan out to companion queries and merge results, de-duplicated, preserving
    each list's ranking (round-robin interleave keeps the main query dominant).

    `extra_queries` lets the caller add its own retrieval probes — used to run
    BOTH the user's natural question and the LLM's keyword rewrite, so recall
    never depends on which of the two the rewriter happened to produce.
    """
    queries = expand_queries(query)

    for q in (extra_queries or []):
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q)

    # Each query is a network round-trip to Pinecone (two, actually — sparse and
    # dense). Sequentially that is ~0.5-1 s each and it dominated the multi-part
    # path; issuing them concurrently makes the fan-out nearly free.
    result_lists = [None] * len(queries)

    def fetch(i, q):
        try:
            return i, retriever.invoke(q)
        except Exception as e:
            print(f"  Retrieval failed for '{q[:48]}...' (non-fatal): {e}")
            return i, []

    if len(queries) == 1:
        result_lists = [fetch(0, queries[0])[1]]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(queries), 8)
        ) as pool:
            for i, docs in pool.map(lambda a: fetch(*a), list(enumerate(queries))):
                result_lists[i] = docs
        result_lists = [r for r in result_lists if r]


    merged = []
    seen = set()

    def key_of(doc):
        md = doc.metadata or {}
        return md.get("chunk_id") or (md.get("source"), md.get("page"), doc.page_content[:120])

    for rank in range(max((len(r) for r in result_lists), default=0)):
        for docs in result_lists:
            if rank < len(docs):
                doc = docs[rank]
                k = key_of(doc)
                if k not in seen:
                    seen.add(k)
                    merged.append(doc)
    return merged


def retrieve_documents(
    query: str,
    top_k: int = None,
    verbose: bool = False,
    user_question: str = None,
    late_query=None,
    timings=None,
    progress=None,
) -> list:


    """
    The single entry point for "given a query, give me the documents".

    Kept separate from the LangGraph node so it can be exercised offline
    (`probe_retrieval.py`) with exactly the same code path the app uses.

    Two query strings, deliberately
    -------------------------------
    `query`         the LLM's optimized/keyword search query
    `user_question` what the user literally typed

    Both are used for retrieval AND for reranking. The keyword form is better at
    lexical recall (acronyms, proper nouns); the natural question is better at
    ranking, because a cross-encoder scores "does this passage answer this
    question" and a bag of keywords is not a question. Relying on either alone
    is what made identical questions succeed only intermittently.

    `late_query` — a rewrite that is still being generated
    -----------------------------------------------------
    Optional callable returning the optimizer's rewrite, or "" if it did not
    arrive in time. It is invoked HERE rather than by the caller, at the last
    moment before the queries are assembled, so the rewriter's round-trip
    overlaps the work this function does first (splitting, expansion) instead of
    happening before any of it. See rag/latency.py.

    `progress` — the waiting student's captions
    -------------------------------------------
    Optional `ProgressChannel`. The two phase boundaries a student most needs to
    see are inside this function, not visible to the caller: "searching" begins
    when the queries are dispatched to Pinecone, and "reading" begins when the
    candidate pool is handed to the cross-encoder. Announcing them from the
    caller would put both captions before either phase started, which is the
    guesswork this avoids. See rag/progress.py.

    Callers that pass nothing behave exactly as before, which is what keeps
    tools/probe_retrieval.py and tools/eval_retrieval.py exercising the same
    ranking code the app uses.
    """

    top_k = top_k or FINAL_TOP_K

    # Collect the rewrite before assembling the phrasings, but AFTER this
    # function has been entered — the caller kicked the LLM off earlier, so
    # whatever it produced is either ready by now or already too late to matter.
    #
    # Appended as one more phrasing rather than substituted for `query`: the
    # caller passes the student's literal question as `query` when it did not
    # wait, and demoting that would undo the fix that stopped identical
    # questions succeeding only intermittently.
    late_phrasing = ""
    if late_query is not None:
        if timings is not None:
            timings.start("await_rewrite")
        try:
            late_phrasing = (late_query() or "").strip()
        except Exception as e:
            # A rewrite is an optimisation. Losing it costs some recall on a thin
            # question, not the answer, so it must never raise into the graph.
            print(f"  Late rewrite unavailable (non-fatal): {e}")
        finally:
            if timings is not None:
                timings.stop("await_rewrite")

    # Both phrasings, de-duplicated, natural question last (it is the fallback
    # AND the ranking signal).
    phrasings = []
    for q in (query, user_question, late_phrasing):
        q = (q or "").strip()
        if q and q not in phrasings:
            phrasings.append(q)

    if not phrasings:
        return []

    primary = phrasings[0]



    # A compound question ("X, Y and Z?") is split so each part can be ranked
    # on its own merits. The natural question is preferred as the split source
    # because it still has its grammar; a keyword rewrite usually does not.
    split_source = user_question or primary
    aspects = split_question(split_source)

    if verbose:
        print(f"\n=== STEP 1: Retrieval (Queries: {phrasings}) ===")
        if aspects:
            print(f"  Multi-part question detected -> {len(aspects)} aspects:")
            for a in aspects:
                print(f"    - {a}")

    # Announced here, immediately before the round-trips, and not a line
    # earlier: everything above is local string work that takes microseconds,
    # so a caption raised before it would be showing "Searching" during
    # splitting and expansion.
    emit_phase(progress, "searching")

    try:
        initial_docs = multi_query_retrieve(
            primary,
            extra_queries=phrasings[1:] + aspects,
        )
        if verbose:
            print(f"  RRF + expansion returned {len(initial_docs)} candidates")
    except Exception as e:
        print(f"  Retrieval failed (non-fatal): {e}")
        initial_docs = []

    # The cross-encoder is next, and on CPU it is usually the longest single
    # phase of the request. The count is the CANDIDATE pool, because that is
    # what is genuinely about to be read; the final selection does not exist
    # yet, and claiming a number now that changes in two seconds would be worse
    # than saying nothing.
    emit_phase(progress, "reading", documents=len(initial_docs))

    if aspects:

        # Round-robin across parts, so no part can be crowded out of the list.
        # The whole question is included as one more "aspect" so documents that
        # answer several parts at once are not penalised.
        final_docs = rerank_multi(
            [*aspects, *phrasings],
            initial_docs,
            top_k=top_k,
            max_pairs=RERANK_CANDIDATES,
        )
    else:
        final_docs = rerank(
            primary,
            initial_docs,
            top_k=top_k,
            max_pairs=RERANK_CANDIDATES,
            queries=phrasings[1:],
        )

    if verbose:
        # When the cross-encoder is off, EVERY document comes back without a
        # score and this list is just the hybrid retriever's own order. Say which
        # of the two you are looking at: an unexplained column of `score=n/a`
        # reads like missing data, and the real cause (no model in the local
        # cache — see download_model.py) is nowhere near this log.
        from rag.reranker import available as _rr_available, unavailable_reason as _rr_reason

        if _rr_available():
            print(f"\n=== STEP 2: Reranked to top {len(final_docs)} ===")
        else:
            print(
                f"\n=== STEP 2: NOT reranked — reranker off ({_rr_reason()}); "
                f"keeping retrieval order, top {len(final_docs)} ==="
            )
        for i, doc in enumerate(final_docs):
            src = doc.metadata.get("source", "Unknown")
            pg = doc.metadata.get("page", "?")
            score = doc.metadata.get("rerank_score")
            score_s = f"{score:.3f}" if isinstance(score, float) else "n/a"

            aspect = doc.metadata.get("rerank_aspect")
            aspect_s = f" | for: {aspect[:40]}" if aspect else ""
            snippet = doc.page_content.replace("\n", " ")[:70]
            print(f"  [{i+1}] score={score_s} | {src} (Pg {pg}){aspect_s}: {snippet}...")

    return final_docs






# --- MODEL INSTANTIATION ---
primary_model = ChatOpenAI(
    model=CHAT_MODEL_NAME,
    openai_api_key=OPENROUTER_API_KEY,
    openai_api_base="https://openrouter.ai/api/v1",
    temperature=0.2,
    extra_body={
        "reasoning": {
            # 1. Base State Control
            "enabled": True,             # Explicitly force reasoning on or off
            
            # 2. Effort Allocation (OpenAI-Style / Normalized)
            # Accepted values: "max", "xhigh", "high", "medium", "low", "minimal", "none"
            "effort": "minimal"
        }
    },
    timeout=30
)

fallback_model = ChatOpenAI(
    model=FALLBACK_MODEL_NAME,
    openai_api_key=OPENROUTER_API_KEY,
    openai_api_base="https://openrouter.ai/api/v1",
    temperature=0.3,
    extra_body={
        "reasoning": {
            # 1. Base State Control
            "enabled": True,             # Explicitly force reasoning on or off
            
            # 2. Effort Allocation (OpenAI-Style / Normalized)
            # Accepted values: "max", "xhigh", "high", "medium", "low", "minimal", "none"
            "effort": "minimal"
        }
    },
    timeout=30
)

chatModel = primary_model.with_fallbacks([fallback_model])


def stream_answer(messages):
    """
    Stream the answer, trying each provider in turn.

    Why this exists instead of `chatModel.stream(...)`
    -------------------------------------------------
    `with_fallbacks` wraps the CALL. A stream is not a call, it is a generator:
    LangChain hands the generator back before the HTTP request is dispatched, so
    the 400 that AgentRouter returns is raised inside the caller's `for` loop,
    long after the fallback wrapper has finished its job. The fallback never
    fires, and the route's `except` turns a recoverable provider refusal into
    "Streaming interrupted." — the exact symptom "latin honor" produced while
    retrieval had already ranked the correct pages (52-56) first.

    So the providers are walked explicitly, and — critically — a provider is only
    abandoned if it failed BEFORE emitting anything. Once the student is reading
    text, restarting on another model would duplicate or contradict the half
    sentence already on screen, which is worse than the error we are fixing.

    Yields text chunks. Raises only if EVERY provider failed at the first token.
    """
    attempts = [("primary", primary_model), ("fallback", fallback_model)]
    last_error = None

    for label, model in attempts:
        produced = False
        try:
            for chunk in model.stream(messages):
                text = chunk.content
                if not text:
                    continue
                if isinstance(text, list):
                    text = "".join(
                        block.get("text", "")
                        for block in text
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                if not text:
                    continue
                produced = True
                yield text
            return  # finished cleanly
        except Exception as e:
            last_error = e
            if produced:
                # Mid-stream failure. The student already has part of the answer;
                # swapping models now would splice two different answers
                # together. Stop and let the caller keep what arrived.
                print(f"  [{label}] failed mid-stream, keeping partial answer: {e}")
                return
            print(f"  [{label}] refused before the first token, trying next: {e}")

    raise last_error if last_error else RuntimeError("No model produced a response.")



summarizer = ChatOpenAI(
    model=SUMMARIZER_MODEL_NAME,
    openai_api_key=OPENROUTER_API_KEY,
    openai_api_base="https://openrouter.ai/api/v1",
    temperature=0,
    extra_body={
        "reasoning": {"enabled": False}
    },
    timeout=30
)

# ====== Chat State ======
class ChatState(TypedDict):
    input: str
    image_data: Optional[str]
    chat_history: List[Dict[str, str]]
    answer: str
    uid: Optional[str]
    user_email: Optional[str]
    data_consent: Optional[bool]
    # Faculty see faculty-only announcements; students never do. Defaults to
    # False, i.e. the student view, because leaking an internal notice to a
    # student is the worse of the two mistakes.
    is_faculty: Optional[bool]
    # Who is asking: "guest" | "student" | "faculty". Shapes WHICH SIDE of a
    # process the answer describes — the steps a student performs, or the steps
    # a teacher performs for their advisees. Absent means student, which is the
    # view that is harmless to show the wrong person. See rag/roles.py.
    role: Optional[str]

    messages_to_llm: Optional[list]

    # The files/pages behind this answer. Carried out of the graph so the route
    # can show them to the student and attach them to a 👍/👎 vote, instead of
    # the citation living only inside the prompt where nobody can check it.
    citations: Optional[List[Dict[str, object]]]

    # NOTE: the progress channel is deliberately NOT a state field.
    #
    # It lived here briefly and every answer died with
    # `Type is not msgpack serializable: ProgressChannel`. The graph is compiled
    # with a checkpointer (below), so the whole state is serialised after each
    # step — and a live `queue.Queue` bound to one open HTTP connection cannot be
    # written to a checkpoint, nor would it mean anything if it were replayed from
    # one. State is the durable record of the conversation; the channel is
    # per-request wiring, so it travels in `config["configurable"]` instead, which
    # is not checkpointed. See `call_llm` and rag/progress.py.





def docs_to_context(docs) -> str:
    """
    Flatten the selected documents into the prompt's context block, labelling
    which of them supersedes the others.

    The label is derived from recorded effective dates, not from a filename.
    What used to be here was:

        is_base = "samar-college-2024.pdf" in source.lower()
        priority_tag = "" if is_base else " [NEW UPDATE - OVERRIDE BASE KNOWLEDGE]"

    That reads as recency but the actual rule is "is not called
    samar-college-2024.pdf". Every consequence follows from that mismatch: a
    freshly uploaded scan of a 2019 memo was tagged as overriding current policy,
    `Samar-College-update-2026.pdf` was ranked against a string literal rather
    than against 2024's real date, and renaming the handbook inverted the base
    case silently. It also fired on a single-document answer, telling the model
    to override knowledge that nothing had contradicted.

    `source_priority()` returns {} unless at least two cited sources have
    DIFFERENT known dates, so the tag now appears only where there is a genuine
    supersession to report — and when it does appear it names the date, so the
    model is being given evidence rather than an instruction to trust.
    """
    priority = source_priority(
        *{(getattr(d, "metadata", None) or {}).get("source", "") for d in docs}
    )

    context_parts = []
    for i, d in enumerate(docs):
        source = d.metadata.get("source", "Unknown")
        page   = d.metadata.get("page", "?")

        rank = priority.get(doc_key(source))
        if not rank:
            # Either a single source, or no dates on record. Both mean there is
            # nothing truthful to say about precedence, so nothing is said.
            priority_tag = ""
        elif rank["is_newest"]:
            priority_tag = f" [MOST RECENT SOURCE — effective {rank['date']}]"
        else:
            priority_tag = (
                f" [SUPERSEDED — effective {rank['date']}; a newer source is "
                f"also quoted below]"
            )

        text   = (
            f"[DOCUMENT {i + 1}]{priority_tag}\n"
            f"Source: {source} | Page: {page}\n"
            f"Content:\n{d.page_content}"
        )
        context_parts.append(text)
    if not context_parts:
        return "No relevant context found."
    return "\n\n---\n\n".join(context_parts)



def summarize_history(history: List[Dict[str, str]]) -> str:
    if not history:
        return ""
    transcript = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    prompt = [
        {"role": "system", "content": "Summarize the conversation to retain key context."},
        {"role": "user",   "content": transcript},
    ]
    return summarizer.invoke(prompt).content


def safe_prompt(template: str, **kwargs) -> str:
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", str(value) if value is not None else "")
    return result


# ====== LangGraph ======
def create_graph():
    graph = StateGraph(ChatState)

    def call_llm(state: ChatState, config=None):
        try:
            user_text  = state["input"]
            image_data = state.get("image_data")
            history    = state.get("chat_history", [])
            uid        = state.get("uid")
            user_email = state.get("user_email")

            # Where to publish "what is happening right now".
            #
            # Read from `config`, NOT from state: state is checkpointed after
            # every step, and a live queue bound to one open HTTP connection is
            # not serialisable — putting it in state made every answer fail with
            # `Type is not msgpack serializable: ProgressChannel`. `configurable`
            # is per-invocation wiring and is never written to a checkpoint,
            # which is exactly what a one-shot pipe to one browser is.
            #
            # None when the graph is driven by a tool instead of the chat route,
            # in which case every emit_phase() below is a no-op — that is what
            # keeps tools/probe_retrieval.py on the same code path as the app.
            # See rag/progress.py.
            progress = (config or {}).get("configurable", {}).get("progress")



            # 🚀 PARALLEL TASK 1: Image Analysis
            def task_image_analysis():
                if not image_data: return None
                vision_msg = HumanMessage(content=[
                    {"type": "text", "text": "Transcribe any text in this image and describe the visual layout in detail."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                ])
                try:
                    desc = summarizer.invoke([vision_msg]).content
                    print(f"Image Analysis: {desc}")
                    return desc
                except Exception as e:
                    print(f"Image analysis failed (non-fatal): {e}")
                    return None

            # 🚀 PARALLEL TASK 2: DynamoDB Student Record Fetch
            def task_student_fetch():
                if not uid: return ""
                try:
                    # Always fetch the record so the system knows WHO they are
                    student = get_student_by_uid(uid)
                    if not student and user_email:
                        student = get_student_by_email(user_email)
                        
                    if student:
                        # If user disabled data consent, provide ONLY basic academic context for personalization
                        if state.get("data_consent") is False:
                            print("  Data consent disabled. Providing basic profile only.")
                            return (
                                "System Note: The user has explicitly opted out of sharing their personal student data. "
                                "Do not provide specific grades, balances, or schedules."
                                "The user has account so he/she is a continuing student already but no further details are available due to privacy settings. "
                                f"Name: {student.get('full_name')}, "
                            )
                        
                        # Full context if consent is enabled
                        print(f"  Student record fetched: {student.get('full_name')} | balance: PHP {student.get('balance', 'N/A')}")
                        return format_student_context(student)
                    else:
                        print("  No student record found for this user.")
                        return f"System Note: User is logged in as {user_email} but no record was found."
                except Exception as e:
                    print(f"  Student record fetch failed (non-fatal): {e}")
                    return ""

            timings = Timings()

            # 🚀 PARALLEL TASK 3: Query Optimization
            #
            # Kicked off with the other two, but NOT waited for here. Retrieval
            # starts on the student's literal words and this rewrite joins as one
            # more probe if it lands inside its budget — see rag/latency.py for
            # why an entire LLM round-trip used to sit in front of every answer.
            def task_query_optimization():
                try:

                    recent_history = "\n".join([f"{m['role'].title()}: {m['content']}" for m in history[-6:]]) if history else "No previous history."
                    context_prompt = [
                        {
                            "role": "system", 
                            "content": (
                                "You are a search query optimizer. Understand the user's intent and the provided context first. Formulate the best search query for RAG that uses sparse and dense retrieval. If you cannot understand the user's intent, return the user's original query.\n"
                                "CRITICAL SEARCH RULES: \n"
                                "1. CONTEXT AWARENESS: Only check chat history if the user asks and it is not a complete question.\n"
                                "3. Do NOT answer the question, just output the optimized search query."
                            )
                        },
                        {"role": "user", "content": f"Chat History:\n{recent_history}\n\nLatest Question: {user_text}"}
                    ]
                    
                    summary_resp = summarizer.invoke(context_prompt).content
                    
                    if isinstance(summary_resp, list):
                        summary_resp = "".join([
                            part.get("text", "") if isinstance(part, dict) else str(part)
                            for part in summary_resp
                        ])
                    elif not isinstance(summary_resp, str):
                        summary_resp = str(summary_resp)
                        
                    q = summary_resp.strip()
                    print(f"  Contextualized Query: {q}")
                    return q
                except Exception as e:
                    print(f"  Contextualization failed (non-fatal): {e}")
                    return user_text

            # 🚀 EXECUTE THE PREPARATION TASKS SIMULTANEOUSLY
            #
            # `with` is NOT used here. A ThreadPoolExecutor context manager joins
            # every worker on exit, which would put the optimizer's round-trip
            # back in front of retrieval — exactly the wait this change removes.
            # The pool is shut down without waiting instead, and the rewrite is
            # collected later (or abandoned) by the callback below.
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
            try:
                future_img     = executor.submit(task_image_analysis)
                future_student = executor.submit(task_student_fetch)

                # The rewrite is only worth waiting for when the question cannot
                # be searched as typed — a follow-up, or too thin. For a
                # self-contained question this skips an entire LLM round-trip,
                # and the literal words retrieve the same documents. The decision
                # lives in rag/latency.py so it is testable without a network.
                wants_rewrite = should_optimize(user_text, history)
                future_query = (
                    executor.submit(task_query_optimization) if wants_rewrite else None
                )

                # Only claimed when the rewrite is genuinely running. On the
                # skip path this phase does not exist, and showing it anyway
                # would be a caption for work that is not happening — the
                # precise failure this design is meant to avoid.
                if wants_rewrite:
                    emit_phase(progress, "understanding")


                # These two ARE waited for, and deliberately: the image
                # description and the student record change what the answer is
                # allowed to say, so proceeding without them would produce a
                # different (wrong) answer rather than a slower one. Only the
                # rewrite is optional, because it only affects recall.
                image_description = future_img.result()
                student_context   = future_student.result()

                if future_query is None:
                    timings.note("optimizer", "skipped")
                    standalone_query = user_text

                    def late_rewrite():
                        return ""
                else:
                    standalone_query = user_text
                    budget = Deadline()

                    def late_rewrite():
                        """
                        The rewrite, if it arrives in time.

                        Retrieval calls this after it has done its own
                        preparation, so the round-trip has already been
                        overlapping useful work. A rewrite later than the budget
                        is dropped rather than cancelled — the call finishes in
                        the background, which is cheaper than throwing away
                        tokens that were nearly ready.
                        """
                        try:
                            rewritten = future_query.result(
                                timeout=budget.remaining()
                            )
                        except concurrent.futures.TimeoutError:
                            timings.note("optimizer", "too-late")
                            print("  Rewrite missed its budget; using the "
                                  "student's own wording.")
                            return ""
                        timings.note("optimizer", "used")
                        # An echo of the input adds a duplicate probe and no
                        # recall, so it is treated as no rewrite at all.
                        picked = pick_search_query(user_text, rewritten)
                        return "" if picked == user_text.strip() else picked

                # === STEPS 1 & 2: Retrieval + Cross-Encoder Reranking ===
                #
                # The literal question leads. The rewrite, when there is one,
                # joins as an additional phrasing inside retrieve_documents() —
                # both are used for retrieval AND reranking, because the rewrite
                # is better at lexical recall while the question is what the
                # cross-encoder needs to judge "does this passage answer this?".
                timings.start("retrieval+rerank")
                final_docs = retrieve_documents(
                    standalone_query,
                    top_k=FINAL_TOP_K,
                    verbose=True,
                    user_question=user_text,
                    late_query=late_rewrite,
                    timings=timings,
                    progress=progress,
                )

                timings.stop("retrieval+rerank")
            finally:
                # Non-blocking: a rewrite still in flight is allowed to finish on
                # its own thread and be discarded. Joining here would reintroduce
                # the wait for a result nobody is going to read.
                executor.shutdown(wait=False)

            # Retrieval is done; everything from here to the answer call is
            # prompt assembly — verified facts, document dates, calendar,
            # announcements, role. Cheap individually, but the history summary
            # below is another LLM call on a long conversation, so this is a real
            # phase and not a rounding error.
            emit_phase(
                progress,
                "preparing",
                documents=len(final_docs),
                sources=len({
                    (getattr(d, "metadata", None) or {}).get("source", "")
                    for d in final_docs
                }),
            )

            # Whatever the optimizer did or did not do, the search text used for
            # the keyword-gated blocks below is what retrieval actually ran with.
            standalone_query = pick_search_query(user_text, standalone_query)


            context_str = docs_to_context(final_docs)


            # === STEP 3: Handle Conversation Summary ===
            if len(history) > 10:
                try:
                    summary = summarize_history(history[:-6])
                    history = [
                        {"role": "system",
                         "content": f"Previous conversation summary: {summary}"}
                    ] + history[-6:]
                except Exception as e:
                    print(f"  History summarization failed (non-fatal): {e}")
                    history = history[-6:]

            history_str = "\n".join(
                [f"{m['role'].upper()}: {m['content']}" for m in history]
            )

            # Admin-verified overrides for anything the corpus contradicts
            # itself about (e.g. two different deans for the same college).
            # Matched against BOTH the question and the retrieved text, so a
            # stale name sitting in the context always arrives with its
            # correction attached instead of being repeated as fact.
            verified_facts = authority_block(user_text, standalone_query, context_str)
            if verified_facts:
                print(f"  Applied admin-verified facts:\n{verified_facts}")

            # Relative age of the documents actually being quoted. This is the
            # fallback for a contradiction nobody has pinned yet: the pinned
            # answer above is absolute, but when there is none, the model should
            # at least know that one of these two pages superseded the other
            # rather than picking whichever chunk scored higher. Emitted only
            # when two cited sources carry different known dates, so an ordinary
            # question pays nothing.
            document_dates = freshness_block(
                *{(getattr(d, "metadata", None) or {}).get("source", "")
                  for d in final_docs}
            )
            if document_dates:
                print(f"  Applied document dates:\n{document_dates}")


            # Timing, pre-computed. The prompt already carries today's date, but
            # a date is not a deadline: "is June 15 still open on June 3, and how
            # many days is that" is arithmetic, and arithmetic is what an LLM
            # does confidently and wrongly. Only questions that mention timing
            # get this block, so ordinary questions pay nothing for it.
            calendar_context = calendar_block(f"{user_text} {standalone_query}")
            if calendar_context:
                print(f"  Applied calendar context:\n{calendar_context}")

            # Live announcements ("classes suspended tomorrow"). Unlike the
            # calendar this is NOT keyword-gated: a suspension is relevant to
            # "is the library open", "should i go to campus" and "what time is my
            # class", and guessing that list in advance is how the one phrasing
            # nobody thought of gets the document's answer during a typhoon.
            #
            # The audience is derived from the role rather than read straight off
            # `is_faculty`, because that flag was never populated by any caller:
            # every request — the registrar's included — resolved to the student
            # feed, which made faculty-scoped notices undeliverable in practice.
            # `is_faculty` is still honoured so an older caller keeps working.
            asker_role = resolve_role(
                state.get("role"),
                is_guest=bool(state.get("role") == "guest"),
            )
            if state.get("is_faculty") and asker_role != "faculty":
                asker_role = "faculty"

            announcements = announcement_block(
                audience=announcement_audience(asker_role)
            )
            if announcements:
                print(f"  Applied announcements:\n{announcements}")

            # Which SIDE of a process to describe. A teacher asking "how do I
            # enroll a student" needs their own steps, not the queue a student
            # stands in. Always present — there is always somebody asking, and
            # leaving it unsaid is what made everyone a student by default.
            asker = role_block(asker_role)
            print(f"  Asker role: {asker_role}")

            # One line, every answer. "It feels slow" is not actionable and a
            # speed change nobody measured is a guess; this is what makes the
            # next person's version of this work start from numbers.
            print(f"  {timings.render()}")


            final_system_prompt = safe_prompt(
                system_prompt,
                retrieved_docs=context_str,
                verified_facts=verified_facts,
                document_dates=document_dates,
                announcements=announcements,
                asker=asker,
                calendar_context=calendar_context,



                chat_history=history_str,
                student_context=student_context,
                current_date=datetime.now().strftime("%B %d, %Y")
            )


            # === STEP 4: Assemble Messages for Flask Streaming ===
            messages = [SystemMessage(content=final_system_prompt)]

            if image_data:
                # 🚀 Inject the parallel-processed image description so the LLM is aware of it
                augmented_text = user_text
                if image_description:
                    augmented_text += f"\n\n[System Note - Image Analysis Provided]: {image_description}"

                content_block = [
                    {"type": "text", "text": augmented_text}, 
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                ]
                messages.append(HumanMessage(content=content_block))
            else:
                messages.append(HumanMessage(content=user_text))

            # Pass compiled state back out to Flask to generate the SSE tokens.
            #
            # `citations` travels with the messages because this is the only
            # place that still knows which documents were selected — once the
            # context has been flattened into a prompt string, the file/page
            # provenance is unrecoverable without re-running retrieval.
            return {
                "messages_to_llm": messages,
                "chat_history": history,
                "citations": build_citations(final_docs),
            }

        except Exception as e:
            import traceback
            print(f"[chain] Unhandled error: {traceback.format_exc()}")
            return {
                "messages_to_llm": [],
                "chat_history": state.get("chat_history", []),
                "citations": [],
            }


    graph.add_node("llm", call_llm)
    graph.set_entry_point("llm")
    graph.add_edge("llm", END)
    return graph.compile(checkpointer=InMemorySaver())

app_graph = create_graph()