"""Optional plain-text query rewriting, matching the reference RAG optimizer."""

from rag.spelling import misspellings

OPTIMIZER_PROMPT = (
    "You are a search query optimizer. Understand the user's intent and the provided context first. Formulate the best search query for RAG that uses sparse and dense retrieval. If you cannot understand the user's intent, return the user's original query.\n"
    "CRITICAL SEARCH RULES: \n"
    "1. CONTEXT AWARENESS: Only check chat history if the user asks and it is not a complete question.\n"
    "2. SPELLING: Fix obvious misspellings of school, academic and program terms so the query uses the words a handbook would use (e.g. 'honir' -> 'honor', 'enrolmnet' -> 'enrollment', 'requirments' -> 'requirements'). Keep correctly spelled words, acronyms and proper nouns exactly as typed.\n"
    "3. Do NOT answer the question, just output the optimized search query."
)


def optimizer_messages(question: str, history=None) -> list:
    recent_history = "\n".join(
        f"{message['role'].title()}: {message['content']}"
        for message in (history or [])[-6:]
        if isinstance(message, dict)
        and message.get("role") in ("user", "assistant", "system")
        and isinstance(message.get("content"), str)
    ) or "No previous history."
    repairs = misspellings(question)
    spelling_note = (
        "\nLIKELY MISSPELLINGS (already checked against the "
        "indexed documents; apply them unless the context says "
        "otherwise): "
        + ", ".join(f"'{typed}' -> '{meant}'" for typed, meant in repairs)
        if repairs else ""
    )
    return [
        {"role": "system", "content": OPTIMIZER_PROMPT},
        {"role": "user", "content": f"Chat History:\n{recent_history}\n\nLatest Question: {question}{spelling_note}"},
    ]


def optimize_query(model, question: str, history=None) -> str:
    try:
        content = model.invoke(optimizer_messages(question, history)).content
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        elif not isinstance(content, str):
            content = str(content)
        query = content.strip()
        print(f"  Contextualized Query: {query}")
        return query
    except Exception as exc:  # noqa: BLE001 - a rewrite failure must not fail the answer.
        print(f"  Contextualization failed (non-fatal): {exc}")
        return question
