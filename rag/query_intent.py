"""Share semantic query intent across retrieval and answer routing."""

import json
import re
from dataclasses import dataclass


INTENT_TERMS = {
    "grading": "grading system",
    "curriculum": "curriculum subjects",
    "deans_list": "Dean's List academic recognition",
    "honors": "Latin honors requirements",
    "history": "history of Samar College",
    "enrollment": "enrollment procedure registration documents steps",
    "family_discount": "family tuition discount",
    "program_offerings": "programs offered",
    "departments": "list of academic departments",
    "leadership": "list academic deans",
    "fees": "tuition fees",
    "scholarship": "scholarship eligibility requirements",
    "calendar": "academic calendar schedule dates",
}


@dataclass(frozen=True)
class QueryIntent:
    name: str
    subject: str = ""


def query_needs_history(question):
    """History resolves missing subjects/references, not spelling or short topics."""
    from rag import source_policy as policy
    from rag.unit_identity import program_dean_subject, request_text

    if not isinstance(question, str) or not question.strip():
        return False
    text = request_text(question)
    references = re.sub(r"\bthis\s+(?:school|college|semester|year|month|week)\b", "", text, flags=re.I)
    # 'IT' is also a program label. Its presence alone is not a pronoun reference.
    references = re.sub(r"\bit\b(?=\s+(?:department|program|courses?|subjects?|curriculum)\b)", "", references, flags=re.I)
    if text == "IT" or (re.search(r"\b(?:classes|courses?|subjects?|study)\b", text, re.I)
                        and re.search(r"\b(?:in|for)\s+IT\b", text)):
        references = re.sub(r"\bIT\b", "", references)
    if policy.curriculum_program(text) or program_dean_subject(text):
        references = re.sub(r"\bit\b", "", references, flags=re.I)
    if re.fullmatch(r"(?:all|everything|all\s+of\s+them|all\s+categories)", text, re.I):
        return True
    if re.search(r"\b(?:it|its|this|that|these|those|they|them|their|he|she|him|her|his|hers)\b", references, re.I):
        return True
    # Explicit topical requests remain independent even when phrased 'what about'.
    if known_query_intents(text):
        return False
    if re.match(r"^(?:(?:what|how)\s+about|and\s+for|same\s+for|the\s+same)\b", text, re.I):
        return True
    if re.fullmatch(
        r"(?:(?:for|at|the)\s+)*(?:graduate(?:\s+(?:school|studies))?|postgraduate|GS|master(?:'s|s)?|"
        r"college|undergrad(?:uate)?|elementary|primary|junior\s+high(?:\s+school)?|senior\s+high(?:\s+school)?|JHS|SHS|basic\s+education)"
        r"(?:\s+level)?", text, re.I,
    ):
        return True
    return bool(re.fullmatch(
        r"\d+|yes|yeah|yep|sure|ok(?:ay)?|no|nope|why|when|where|how|how\s+(?:much|many)|"
        r"(?:what\s+(?:are|is)\s+)?(?:the\s+)?(?:requirements?|procedures?|criteria|eligibility)", text, re.I,
    ))


def decode_optimizer_output(text, fallback):
    """Accept structured intent, retaining plain-text model/provider fallbacks."""
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text.strip(), re.I)
    payload = fenced.group(1) if fenced else text
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return (fallback if payload.lstrip().startswith(("{", "[", "```")) else text), None
    if isinstance(data, list):
        return fallback, None
    if not isinstance(data, dict):
        return text, None
    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        return fallback, None
    name, subject = data.get("intent"), data.get("subject", "")
    intent = None
    if isinstance(name, str) and name in INTENT_TERMS and isinstance(subject, str) and len(subject) <= 200:
        intent = QueryIntent(name, subject.strip())
    return query.strip(), intent


def known_query_intents(question):
    """The same explicit-topic boundary protects semantic routing and search hints."""
    from rag import source_policy as policy
    from rag.unit_identity import program_dean_subject, program_overview_subject

    detectors = (
        ("deans_list", policy.deans_list_queries), ("grading", policy.grading_queries),
        ("curriculum", policy.curriculum_queries), ("history", policy.institution_history_queries),
        ("honors", policy.honors_queries), ("family_discount", policy.family_discount_queries),
        ("enrollment", policy.enrollment_queries), ("departments", policy.department_catalog_queries),
        ("program_offerings", policy.program_catalog_queries), ("leadership", policy.leadership_roster_queries),
        ("leadership", program_dean_subject), ("general", program_overview_subject),
    )
    return {name for name, detector in detectors if detector(question)}


def resolve_query_intent(question, intent=None, history=None):
    """Supplement unknown phrasing; explicit recognized requests keep precedence."""
    from rag import source_policy as policy
    from rag.unit_identity import is_unit_reference, requested_unit

    if not isinstance(question, str) or not question.strip():
        return question
    history = history if query_needs_history(question) else None
    if re.fullmatch(r"(?:all|everything|all\s+of\s+them|all\s+categories)", question.strip(), re.I):
        previous = next((message.get("content", "") for message in reversed(history or [])
                         if isinstance(message, dict) and message.get("role") == "user"
                         and isinstance(message.get("content"), str)
                         and not re.fullmatch(r"(?:all|everything|all\s+of\s+them|all\s+categories)",
                                              message["content"].strip(), re.I)), "")
        if previous:
            if policy.enrollment_queries(previous):
                return ("all enrollment requirements and complete enrollment procedure for new students, "
                        "transferees, old students and continuing students, including every final step")
            if policy.curriculum_queries(previous):
                return f"{previous} all years and semesters, complete curriculum subjects"
            if policy.grading_queries(previous):
                return f"{previous} complete grading scale, computation rules and special marks"
            if policy.deans_list_queries(previous):
                return f"{previous} complete eligibility and recognition details"
            return f"{previous} complete details for all applicable categories"
        return question
    resolved = policy.resolve_grading_followup(question, history)
    if resolved != question or not isinstance(intent, QueryIntent) or intent.name not in INTENT_TERMS:
        return resolved
    if known_query_intents(question):
        return question
    # Semantic metadata cannot change private-record requests, exclusions, or
    # a named standalone topic into a different public-policy question.
    if (re.search(r"\b(?:my|own|personal)\b.{0,60}\b(?:grades?|marks?|subjects?|schedule|balance|fees?|payments?|records?|gpa|gwa|history)\b", question, re.I)
            or re.search(r"\b(?:not|no|never|without|except|excluding|instead of|rather than)\b|\bdon['\u2019]t\b", question, re.I)
            or re.search(r"\b(?:deans?|principals?|presidents?|heads?|directors?)\b", question, re.I)
            or (intent.name != "departments" and re.search(r"^\s*(?:list|show|give)\b.{0,50}\b(?:departments?|colleges?)\b", question, re.I))
            or (intent.name != "program_offerings" and re.search(r"^\s*(?:list|show|give)\b.{0,50}\b(?:programs?|degrees?)\b", question, re.I))
            or re.search(r"\b(?:and|or)\b|;", question, re.I)
            or len(re.findall(r"\w+", question)) < 2):
        return question
    # Named subjects must be copied from the question, or the recent user turn
    # for an actual follow-up. Assistant prose never establishes an identity.
    scope = question
    if history and is_unit_reference(question):
        previous_user = next((message["content"] for message in reversed(history[-6:])
                              if isinstance(message, dict) and message.get("role") == "user"
                              and isinstance(message.get("content"), str)), "")
        scope += "\n" + previous_user
    if intent.subject and not re.search(rf"(?<!\w){re.escape(intent.subject)}(?!\w)", scope, re.I):
        return question
    if intent.name == "curriculum":
        if not intent.subject or not policy.curriculum_program(f"{intent.subject} subjects"):
            return question
    unit = requested_unit(question)
    constraints = re.findall(
        r"\b(?:first|second|third|fourth|[1-4](?:st|nd|rd|th)?)\s+(?:year|sem(?:ester)?)\b|"
        r"\b(?:year|sem(?:ester)?)\s+[1-4]\b|\b\d+(?:\.\d+)?\b|"
        r"\b(?:historical|previous|old|compare|comparison|versus|transferee|transfer|returning|new|international)\b",
        question, re.I,
    )
    level = policy._grading_level(question)
    if not level and re.search(r"\b(?:undergrad(?:uate)?s?|college|collegiate|tertiary)\b", question, re.I):
        level = "undergraduate"
    # Route by semantic topic plus explicit scope, while retaining the complete
    # corrected question separately for retrieval and generation. Free-form
    # request verbs must not accidentally disable the selected topic handler.
    return " ".join(dict.fromkeys(part for part in (
        intent.subject, level, INTENT_TERMS[intent.name], unit.name if unit else "", *constraints,
    ) if part))
