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
    "financial_aid": "available tuition discounts scholarships grants financial aid",
    "program_offerings": "programs offered",
    "departments": "list of academic departments",
    "leadership": "list academic deans",
    "fees": "tuition fees",
    "scholarship": "scholarship eligibility requirements",
    "calendar": "academic calendar schedule dates",
    "student_id": "lost student ID RFID identification card replacement procedure",
    "campus_navigation": "I am lost on campus Samar College directions",
    "campus_location": "Samar College address",
}

_SIBLING_FOLLOWUP = re.compile(r"(?:(?:(?:what|how)\s+about|and)\s+)?(?:for\s+)?siblings?", re.I)
_FOLLOWUP_LEAD = re.compile(r"^(?:(?:can|could|would)\s+you\s+)?(?:(?:show|tell|give)\s+me\s+)?", re.I)
_FOLLOWUP_FACETS = (
    ("amount or rates", r"how\s+much(?:\s+(?:is\s+(?:it|that)|(?:it|that)\s+is))?"
     r"|what(?:'s|\s+is)\s+the\s+(?:amount|rates?|prices?|cost|total)"
     r"|(?:the\s+)?(?:amount|rates?|prices?|cost|total)(?:\s+for\s+(?:it|that))?"
     r"|(?:magkano|pila)(?:\s+(?:ito|iyan|ini|an\s+bayad))?"),
    ("eligibility requirements", r"(?:what\s+(?:are|is)\s+)?(?:(?:the|its)\s+)?(?:requirements?|criteria|eligibility)"
     r"|what\s+do\s+i\s+need|who\s+(?:is\s+eligible|qualifies)|can\s+i\s+(?:apply|qualify)"
     r"|am\s+i\s+(?:eligible|qualified)|(?:do|will)\s+i\s+qualify|are\s+(?:they|we)\s+eligible"
     r"|(?:ano\s+ang|ano\s+an)\s+(?:mga\s+)?requirements?"),
    ("required documents", r"(?:what\s+)?(?:documents?|paperwork)(?:\s+do\s+i\s+need)?"
     r"|what\s+(?:should|do)\s+i\s+(?:bring|submit)"),
    ("application or procedure steps", r"(?:what\s+(?:are|is)\s+)?(?:(?:the|its)\s+)?(?:steps?|procedures?)"
     r"|how\s+(?:do|can|should)\s+i\s+(?:apply|register|do\s+(?:it|that))"
     r"|how\s+does\s+(?:it|that)\s+work|paano\s+(?:mag-?apply|ito)"),
    ("start or opening date", r"(?:what\s+is\s+(?:the|its)\s+)?(?:start|opening)\s+date|when\s+does\s+(?:it|that)\s+(?:start|open)"),
    ("processing time or duration", r"how\s+long(?:\s+(?:does|will)\s+(?:it|that)\s+take)?|what\s+is\s+the\s+duration"),
    ("deadline or closing date", r"(?:what\s+is\s+(?:the|its)\s+)?(?:deadline|closing\s+date)|when\s+(?:does\s+(?:it|that)\s+(?:end|close)|is\s+it\s+due)"),
    ("dates or schedule", r"(?:what\s+(?:are|is)\s+)?(?:(?:the|its)\s+)?(?:dates?|schedule)|when(?:\s+is\s+(?:it|that))?|kailan(?:\s+(?:ito|iyan))?|san-o(?:\s+(?:ini|ito))?"),
    ("location", r"where(?:\s+(?:is\s+(?:it|that)|do\s+i\s+go))?|saan(?:\s+(?:ito|iyan))?|diin(?:\s+(?:ini|ito))?"),
    ("count", r"how\s+many(?:\s+are\s+there)?"),
    ("example", r"(?:(?:an?|some)\s+)?examples?(?:\s+of\s+(?:it|that))?"),
    ("more details", r"(?:more\s+(?:details|information|on\s+(?:it|that))|(?:explain|elaborate)(?:\s+(?:it|that|more|on\s+(?:it|that)))?)"),
)
_FOLLOWUP_FACETS = tuple((facet, re.compile(pattern, re.I)) for facet, pattern in _FOLLOWUP_FACETS)
_FOLLOWUP_GROUP = re.compile(
    r"(?:(?:what|how)\s+about\s+|and\s+|same\s+)?for\s+"
    r"(?P<group>(?:incoming|new|first-year|continuing|returning|old|international|graduate|undergraduate)\s+students?"
    r"|transferees?|transfers?|freshm[ae]n|parents?|students?|faculty|staff)", re.I,
)


def _followup_focus(text):
    text = _FOLLOWUP_LEAD.sub("", text)
    text = re.sub(r"^(?:what|how)\s+about\s+(?!for\b)", "", text, flags=re.I)
    for facet, pattern in _FOLLOWUP_FACETS:
        if pattern.fullmatch(text):
            return facet
    return ""


# A question about one specific room or place must not be rewritten into the school's
# address or an "I am lost" request, whatever label the optimizer attached to it.
_SPECIFIC_PLACE = re.compile(
    r"\b(?:class\s*rooms?|rooms?|labs?|laborator(?:y|ies)|gym(?:nasium)?|canteen|cafeteria|comfort\s+rooms?|"
    r"rest\s*rooms?|toilets?|halls?|buildings?|floors?|stage|quadrangle|auditorium)\b", re.I)


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
    if policy.campus_location_queries(text):
        return False
    references = re.sub(r"\bthis\s+(?:school|college|semester|year|month|week)\b", "", text, flags=re.I)
    # 'IT' is also a program label. Its presence alone is not a pronoun reference.
    references = re.sub(r"\bit\b(?=\s+(?:department|program|courses?|subjects?|curriculum)\b)", "", references, flags=re.I)
    if text == "IT" or (re.search(r"\b(?:classes|courses?|subjects?|study)\b", text, re.I)
                        and re.search(r"\b(?:in|for)\s+IT\b", text)):
        references = re.sub(r"\bIT\b", "", references)
    if policy.curriculum_program(text) or program_dean_subject(text):
        references = re.sub(r"\bit\b", "", references, flags=re.I)
    if (_followup_focus(text) or _FOLLOWUP_GROUP.fullmatch(text) or _SIBLING_FOLLOWUP.fullmatch(text)
            or re.fullmatch(r"(?:all|everything|all\s+of\s+them|all\s+categories)", text, re.I)):
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
        r"\d+|yes|yeah|yep|sure|ok(?:ay)?|no|nope|why|when|where|how|how\s+many|"
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
        ("campus_location", policy.campus_location_queries),
        ("campus_navigation", policy.campus_navigation_queries),
        ("student_id", policy.student_id_queries),
        ("dropped_subject", policy.dropped_subject_queries),
        ("classroom_location", policy.classroom_location_queries),
        ("deans_list", policy.deans_list_queries), ("grading", policy.grading_queries),
        ("curriculum", policy.curriculum_queries), ("history", policy.institution_history_queries),
        ("honors", policy.honors_queries), ("family_discount", policy.family_discount_queries),
        ("financial_aid", policy.broad_aid_queries),
        ("enrollment", policy.enrollment_queries), ("departments", policy.department_catalog_queries),
        ("program_offerings", policy.program_catalog_queries), ("leadership", policy.leadership_roster_queries),
        ("leadership", program_dean_subject), ("general", program_overview_subject),
    )
    return {name for name, detector in detectors if detector(question)}


def query_rewrite_supported(question, rewritten, history=None, *, preserve_topic=True):
    """Reject subject invention independently of whether the original has a known intent."""
    from rag.source_policy import _grading_level, broad_aid_queries, grading_queries, student_card_mentioned

    explicit = known_query_intents(question)
    if grading_queries(question):
        if not grading_queries(rewritten):
            return False
        original_level = _grading_level(question) or "college"
        rewritten_level = _grading_level(rewritten) or "college"
        if original_level != rewritten_level:
            return False
    if "financial_aid" in explicit and not broad_aid_queries(rewritten):
        return False
    if preserve_topic and explicit and not explicit.intersection(known_query_intents(rewritten)):
        return False
    if student_card_mentioned(rewritten) and not student_card_mentioned(question):
        # Only a real incomplete follow-up may recover the card from a USER turn.
        previous_user = next((message["content"] for message in reversed((history or [])[-6:])
                              if isinstance(message, dict) and message.get("role") == "user"
                              and isinstance(message.get("content"), str)), "") if query_needs_history(question) else ""
        if not student_card_mentioned(previous_user):
            return False
    return True


def resolve_family_discount_followup(question, history):
    """Carry forward the nearest user-established family discount topic."""
    from rag.source_policy import broad_aid_queries, family_discount_queries
    from rag.unit_identity import request_text

    text = request_text(question)
    asks_amount = _followup_focus(text) == "amount or rates"
    sibling_scope = bool(_SIBLING_FOLLOWUP.fullmatch(text))
    if not (asks_amount or sibling_scope):
        return question
    for message in reversed((history or [])[-6:]):
        if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        previous = request_text(message["content"])
        if not previous or _followup_focus(previous):
            continue
        if _SIBLING_FOLLOWUP.fullmatch(previous):
            sibling_scope = True
            continue
        if family_discount_queries(previous) or (sibling_scope and broad_aid_queries(previous)):
            if asks_amount:
                return "sibling tuition discount rates by number of enrolled family members"
            return "Is there a family tuition discount for siblings?"
        break
    return question


def resolve_topic_followup(question, history):
    """Attach a topic to stand-alone follow-up fragments using recent user turns."""
    from rag.unit_identity import request_text

    text = request_text(question)
    focus = _followup_focus(text)
    group_match = _FOLLOWUP_GROUP.fullmatch(text)
    if not (focus or group_match):
        return question

    family = resolve_family_discount_followup(question, history)
    if family != question:
        return family

    group = group_match["group"] if group_match else ""
    for message in reversed((history or [])[-6:]):
        if not isinstance(message, dict) or message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        previous = request_text(message["content"])
        if not previous or re.fullmatch(r"(?:ok(?:ay)?|thanks|thank\s+you|got\s+it)", previous, re.I):
            continue
        if previous == text:
            break
        previous_focus = _followup_focus(previous)
        if previous_focus:
            if focus and focus != previous_focus:
                return question
            focus = focus or previous_focus
            continue
        previous_group = _FOLLOWUP_GROUP.fullmatch(previous)
        if previous_group:
            group = group or previous_group["group"]
            continue
        if query_needs_history(previous):
            # An unresolved reference may have changed the subject. Let the
            # model resolve it from the full exchange instead of guessing here.
            break
        if group:
            # A later group replaces an earlier trailing group in the same topic.
            for match in re.finditer(r"\bfor\b", previous, re.I):
                if _FOLLOWUP_GROUP.fullmatch(previous[match.start():]):
                    previous = previous[:match.start()].rstrip()
                    break
        suffix = " ".join(part for part in (focus, f"for {group}" if group else "") if part)
        return f"{previous} ({suffix})"
    return question


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
            if policy.broad_aid_queries(previous):
                return previous
            return f"{previous} complete details for all applicable categories"
        return question
    if (isinstance(intent, QueryIntent) and intent.name in {"campus_location", "campus_navigation"}
            and policy.office_location_subject(question)):
        # "Where is the library/clinic/cashier?" asks about ONE place. Rewriting it to
        # the school's own address drops the place and matches any 'LOCATION' chunk.
        return question
    resolved = resolve_family_discount_followup(question, history)
    if resolved != question:
        return resolved
    resolved = policy.resolve_grading_followup(question, history)
    if resolved != question or not isinstance(intent, QueryIntent) or intent.name not in INTENT_TERMS:
        return resolved
    if known_query_intents(question):
        return question
    # Semantic metadata cannot change private-record requests, exclusions, or
    # a named standalone topic into a different public-policy question.
    if (re.search(r"\b(?:my|own|personal)\b.{0,60}\b(?:grades?|marks?|subjects?|schedule|balance|fees?|payments?|records?|gpa|gwa|history|class\s*rooms?|rooms?)\b", question, re.I)
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
    if intent.name == "student_id" and not policy.student_id_queries(question):
        return question
    if intent.name in {"campus_navigation", "campus_location"} and _SPECIFIC_PLACE.search(question):
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
