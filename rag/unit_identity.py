"""Conservative, single-subject unit identity checks without a school-name table."""

import re
import unicodedata
from copy import copy
from dataclasses import dataclass

from rag.spelling import is_known_acronym, program_code_candidates

_HEAD = re.compile(
    r"\b(?:college|school|department|faculty|institute|office|center|centre|university)\s+(?:of|for)\s+|"
    r"\bgraduate\s+school\b|\bsamar\s+colleges?\s+technological\s+institute\b",
    re.IGNORECASE,
)
_TAIL = re.compile(
    r"\b(?:offers?|offered|provides?|includes?|has|have|is|are|was|were|does|do|did|who|what|where|"
    r"when|why|how|which|at)\b|"
    r"\b(?:in|during|as of|from|for(?:\s+(?:the\s+)?(?:school|academic)\s+year)?)\s+20\d{2}\b", re.IGNORECASE,
)
_POLICY_TOPIC = r"(?:grading|grades?|tuition|fees?|curriculum|schedule|policy|requirements?|admission|enrollment|registration|scholarships?|discounts?|subjects?|courses?)"
_TOPIC_TAIL = re.compile(
    rf"\b{_POLICY_TOPIC}\b(?:\s+(?:and|or)\s+\b{_POLICY_TOPIC}\b)*"
    r"(?:\s+(?:systems?|scales?|tables?|guidelines?|process(?:es)?|procedures?))*", re.IGNORECASE,
)
_CONNECTORS = {"of", "for", "and", "the", "a", "an"}
_GENERIC = {"it", "me", "us", "we", "my", "what", "which", "who", "where", "when", "how", "why",
            "college", "school", "campus", "office", "faculty", "unit", "dean", "head", "staff",
            "fee", "fees", "cost", "help", "program", "programs", "course", "courses", "gpa", "gwa", "id",
            "in", "on", "at", "by", "from", "with", "to", "than", "under", "art", "arts", "name",
            "current", "new", "old", "latest", "history", "academic", "for", "of", "and", "or", "the", "a", "an",
            "all", "every", "each", "list", "show", "tell", "please", "pls", "plz"}
_GENERIC |= {word + "s" for word in _GENERIC}
_HEADINGS = ("collection_heading", "section_heading", "section", "header")
# Explicit institutional labels only; never drop arbitrary name qualifiers.
_UNIT_ALIASES = {
    "coed": "College of Education",
    "clas": "College of Liberal Arts and Sciences",
    "college of liberal arts": "College of Liberal Arts and Sciences",
    "scti": "Samar College Technological Institute",
}
_INDEX_CONTEXT_ALIASES = {
    "gs": "Graduate School",
    "citas": "College of Information Technology and Allied Sciences",
    "cocje": "College of Criminal Justice Education",
}
_ROLE_PREFIX = (
    r"(?:the\s+)?(?:(?:oic|acting|interim|assistant|associate)[\s-]+)*"
    r"(?:deans?|heads?|chairs?|chairpersons?|principals?|directors?|registrars?)"
    r"(?:\s+(?:of|for|at)\s+|\s*[,:-]\s*|\s+(?=(?:college|school|department|faculty)\b))"
)
_ROSTER_HEADING = re.compile(
    r"\b(?:academic\s+council|personnel|staff|faculty\s+roster|leadership|administration|officials?|"
    r"organizational\s+structure|colleges\s+(?:&|and)\s+academic\s+units|deans?\s+(?:update|roster|list))\b", re.IGNORECASE,
)


def request_text(question):
    """Normalize conversational edges and role order for parsing, not stored input."""
    text = (question or "").strip(" .,?!\t\r\n")
    courtesy = r"(?:please|pls|plz|kindly|thanks|thank\s+you|(?:can\s+you\s+)?tell\s+me)"
    # Spelling repair can leave an incomplete identity question ("who is?").
    # Strip its grammar, but not another named subject such as "who is president".
    identity_tail = r"who(?:\s+(?:is|are|was|were)(?:\s+(?:it|that|he|she|they))?)?"
    while text:
        cleaned = re.sub(rf"^{courtesy}\b[\s,]*", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(rf"[\s,]+(?:{courtesy}|{identity_tail})$", "", cleaned,
                         flags=re.IGNORECASE).strip(" .,?!\t\r\n")
        if cleaned == text:
            break
        text = cleaned
    reverse = re.fullmatch(
        r"(?:(?:who|what)\s+(?:is|are)\s+(?:the\s+)?)?(.+?)(?:['\u2019]s)?\s+"
        r"(deans?|heads?|chairs?)(\s+(?:in|during|as of|for academic year)\s+20\d{2}\b.*)?",
        text, re.IGNORECASE,
    )
    if reverse and (_HEAD.search(reverse[1]) or re.match(r"^(?:bachelor|master|doctor|associate)\b|^(?:ba|bs|ma)\s+", reverse[1], re.IGNORECASE) or (
        re.fullmatch(r"[a-z]{2,12}", reverse[1], re.IGNORECASE)
        and reverse[1].casefold() not in _GENERIC
        and (is_known_acronym(reverse[1]) or reverse[1].isupper() or program_code_candidates(reverse[1]))
    )):
        text = f"{reverse[2]} of {reverse[1]}{reverse[3] or ''}"
    return text


@dataclass(frozen=True)
class UnitRequest:
    name: str
    remainder: str
    explicit: bool = True

    @property
    def protected(self):
        ordinary = {"can", "car", "cars", "cat", "cats", "cap", "caps", "care", "case", "cash", "code", "cost"}
        return (self.explicit or self.name.isupper() or sum(char.isupper() for char in self.name) > 1
                or is_known_acronym(self.name)
                or (re.fullmatch(r"c[a-z]{1,3}", self.name.casefold()) is not None
                    and self.name.casefold() not in ordinary))


def _tokens(name):
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", name).casefold())
    return tuple(word for word in words if word not in _CONNECTORS)


def _plural_equal(left, right):
    if left == right:
        return True
    short, long = sorted((left, right), key=len)
    return (len(short) > 2 and (long == short + "s" or long == short + "es"
                              or (short.endswith("y") and long == short[:-1] + "ies")))


def same_unit(left, right):
    """Allow casing/ordinary number variants, never added/dropped name qualifiers."""
    left = _UNIT_ALIASES.get(left.strip().casefold(), left)
    right = _UNIT_ALIASES.get(right.strip().casefold(), right)
    a, b = _tokens(left), _tokens(right)
    if not _HEAD.match(left) or not _HEAD.match(right):
        return not _HEAD.match(left) and not _HEAD.match(right) and a == b
    return len(a) == len(b) and all(_plural_equal(x, y) for x, y in zip(a, b))


def is_unit_reference(question):
    if re.search(r"\bit\s+(?:possible|necessary|required|allowed|important|true|mandatory)\b", question, re.IGNORECASE):
        return False
    return bool(re.search(r"\b(?:it|its)\b|\b(?:that|this|same)\s+(?:college|department|unit)\b", question, re.IGNORECASE)
                or re.match(r"\s*(?:and|also|what about|how about)\b", question, re.IGNORECASE))


def _names(text):
    heads = []
    for head in _HEAD.finditer(text):
        if heads and head.start() == heads[-1].end() and head[0].casefold().startswith("graduate"):
            continue
        heads.append(head)
    result = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        body = text[head.end():end]
        punctuation = re.search(r"[\n?!;:.>|()\[\]]", body)
        if punctuation:
            end = head.end() + punctuation.start()
            body = text[head.end():end]
        for tail in _TAIL.finditer(body):
            if tail.start() > 0:
                end = head.end() + tail.start()
                break
        topic = _TOPIC_TAIL.search(body)
        if topic and topic.start() > 0 and not body[topic.end():].strip(" ?.!,;:"):
            end = min(end, head.end() + topic.start())
        name = text[head.start():end].strip(" ,\t")
        name = re.sub(r"(?:\s+(?:and|or|the|a|an))+$", "", name, flags=re.IGNORECASE)
        if len(_tokens(name)) >= 2 and len(name) <= 180:
            result.append((name, head.start(), head.start() + len(name)))
    return result


def requested_unit(question):
    """Only bind a single explicit subject; do not collapse compound requests."""
    question = request_text(question)
    if any(is_known_acronym(match[1]) for match in re.finditer(
        r"\b(?:and|or|versus|vs)\s+([a-z]{2,12})\b", question, re.IGNORECASE,
    )):
        return None
    names = _names(question)
    if len(names) == 1:
        name, start, end = names[0]
        remainder = question[:start] + " " + question[end:]
        compound = re.search(r"\b(?:and|or|versus|vs)\b|;", remainder, re.IGNORECASE)
        topic_suffix = _TOPIC_TAIL.search(question)
        topic_compound = (compound and topic_suffix and topic_suffix.start() >= end
                          and re.search(r"\b(?:and|or)\b", topic_suffix[0], re.IGNORECASE))
        if not compound or topic_compound:
            return UnitRequest(name, remainder)
    if names:
        return None
    listing = re.fullmatch(r"([a-z]{2,8})\s+(?:program(?:me)?s?|courses?|training(?:\s+programs?)?|"
                           r"qualifications?(?:\s+titles?)?)(?:\s+(?:offered|offerings?|list))?[?.!]*", question, re.IGNORECASE)
    if listing and listing[1].casefold() in _UNIT_ALIASES:
        return UnitRequest(listing[1], question[listing.end(1):])
    # Bare short identifiers are tentative until the retrieved units establish
    # an actual initialism. This does not turn ordinary words into departments.
    bare = re.fullmatch(r"\s*(?:(?:i mean|tell me about|what is|what about|about)\s+)?([a-z]{2,8})[?.!]*", question, re.IGNORECASE)
    if bare and bare[1].casefold() not in _GENERIC:
        return UnitRequest(bare[1], "", False)
    for pattern, subject in (
        (r"\b(?:deans?|heads?|chairs?)\s+(?:of|for|at)\s+([a-z]{2,8})\b", True),
        (r"\b(?:departments?|colleges?|units?)\s+([a-z]{2,8})\b", False),
        (r"\b(?:programs?|courses?|training|qualifications?(?:\s+titles?)?)\s+(?:does|do)\s+([a-z]{2,8})\s+(?:offer|have)\b", True),
    ):
        match = re.search(pattern, question, re.IGNORECASE)
        if match and match[1].casefold() not in _GENERIC:
            if not subject and not (match[1].isupper() or sum(char.isupper() for char in match[1]) > 1):
                continue
            remainder = question[:match.start(1)] + " " + question[match.end(1):]
            if not re.search(r"\b(?:and|or|versus|vs)\b|;", remainder, re.IGNORECASE):
                return UnitRequest(match[1], remainder)
    return None


def _sections(document):
    metadata = getattr(document, "metadata", None) or {}
    headings = [str(metadata.get(key) or "") for key in _HEADINGS]
    text = getattr(document, "page_content", "") or ""
    text = re.sub(r"\[(?:SECTION|TABLE context):\s*([^\]]+)\]", r"\1\n", text, flags=re.IGNORECASE)
    rows = [line.strip().lstrip(" #*>\t-0123456789.)\u2022\u25aa\u25e6") for line in re.split(r"\n|\|", text)]
    if (any(_ROSTER_HEADING.search(heading) for heading in headings)
            or any(_ROSTER_HEADING.fullmatch(line) for line in rows)):
        # Indexed roster rows can place a name and multiple roles on one line.
        # This extracts affiliations for identity matching only; names and role
        # pairings remain in the unchanged source text used to answer.
        affiliations = [
            re.sub(r"\s+Member\s*$", "", line[match.start():], flags=re.IGNORECASE)
            for line in rows for match in re.finditer(_ROLE_PREFIX, line, re.IGNORECASE)
        ]
        # Dean-update lists use 'Name - College of ...' instead of a role prefix.
        affiliations.extend(
            line[head.start():] for line in rows for head in _HEAD.finditer(line)
            if re.search(r"[-:\u2013\u2014]\s*$", line[:head.start()])
        )
        rows.extend(affiliations)
    # Roster designations commonly use "Dean, College of ...", not "Dean of ...".
    # Retain the original document so the adjoining name stays paired with its role.
    rows = [line for line in rows if _HEAD.match(line) or re.match(_ROLE_PREFIX, line, re.IGNORECASE)
            or any(re.match(rf"^{re.escape(code)}\s+(?:programs?|courses?|training|qualifications?)\b", line, re.IGNORECASE)
                   for code in _UNIT_ALIASES if " " not in code)]
    return [*headings, *rows]


def document_unit_names(document):
    names = []
    for value in _sections(document):
        names.extend(name for name, _, _ in _names(value))
        for code, name in _UNIT_ALIASES.items():
            if re.match(rf"^(?:{_ROLE_PREFIX})?{re.escape(code)}"
                        r"(?:\s*$|\s*[:(]|\s+(?:department|college|programs?|courses?|training|qualifications?|staff|faculty|curriculum)\b)",
                        value.strip(), re.IGNORECASE):
                names.append(name)
    return tuple(names)


def unit_query_variants(question):
    """Expand a bound abbreviation for search, without editing the user's text."""
    request = requested_unit(question)
    name = _UNIT_ALIASES.get(request.name.casefold()) if request else None
    if not name:
        return []
    return [re.sub(rf"\b{re.escape(request.name)}\b", name, question, flags=re.IGNORECASE)]


def unit_context_alias(text):
    """Return the canonical unit name and recognized acronym for indexed context."""
    from rag.spelling import is_known_acronym

    text = str(text or "").replace("_", " ")
    for code, name in (*_INDEX_CONTEXT_ALIASES.items(), *_UNIT_ALIASES.items()):
        if " " in code or not is_known_acronym(code):
            continue
        if re.search(rf"\b{re.escape(code)}\b|\b{re.escape(name)}\b", text, re.I):
            display = {"coed": "CoEd"}.get(code, code.upper())
            return name, display
    return None


def document_matches_unit(document, request):
    """Use named sections/rows, not word overlap or an incidental mention."""
    names = document_unit_names(document)
    if _HEAD.match(request.name):
        return any(same_unit(request.name, name) for name in names)
    code = request.name.casefold()
    alias = _UNIT_ALIASES.get(code)
    if alias and any(same_unit(alias, name) for name in names):
        return True
    if request.protected and any("".join(word[0] for word in _tokens(name)) == code for name in names):
        return True
    if request.explicit:
        subject = re.compile(
            rf"^(?:{_ROLE_PREFIX})?{re.escape(request.name)}"
            r"(?:\s*$|\s*[:(]|\s+(?:department|college|program|staff|faculty|curriculum)\b)", re.IGNORECASE,
        )
        return any(subject.match(line.strip()) for line in _sections(document))
    return False


def program_dean_subject(question):
    """A potential program identifier, not an assertion that it is a college."""
    text = request_text(question)
    match = re.search(r"\bdeans?\s+(?:of|for|in|at)\s+(?:the\s+)?(.+)$", text, re.IGNORECASE)
    if not match:
        return None
    subject = re.split(r"[?!;\n]|\b(?:in|during|as of|for academic year)\s+20\d{2}\b|\s+at\s+", match[1], maxsplit=1,
                       flags=re.IGNORECASE)[0].strip(" .,\t")
    explicit_program = bool(re.match(r"(?:program|course|degree)\s+(?:in|of|for)?\s*", subject, re.IGNORECASE))
    subject = re.sub(r"^(?:program|course|degree)\s+(?:(?:in|of|for)\s+)?", "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"\s+(?:program|course|degree)$", "", subject, flags=re.IGNORECASE)
    if _HEAD.search(subject):
        return None
    if re.match(r"^(?:bachelor|master|doctor|associate)\b", subject, re.IGNORECASE):
        coordinated = re.search(r"\b(?:and|or|also|versus|vs)\s+(?:the\s+)?"
                                r"(?:bachelor|master|doctor|associate|deans?|president)\b", subject, re.IGNORECASE)
        if coordinated or any(is_known_acronym(match[1]) for match in re.finditer(
            r"\b(?:and|or|also|versus|vs)\s+([a-z]{2,12})\b", subject, re.IGNORECASE,
        )):
            return None
        return subject
    if re.search(r"\b(?:and|or|also|versus|vs)\b", subject, re.IGNORECASE):
        return None
    # Degree-code prefixes are candidates only; the catalog must prove them.
    if (re.fullmatch(r"[a-z]{2,12}(?:\s+[a-z][a-z -]{1,60})?", subject, re.IGNORECASE)
            and subject.casefold() not in _GENERIC | _UNIT_ALIASES.keys()
            and (explicit_program or re.match(r"^[bmad]", subject, re.IGNORECASE) or program_code_candidates(subject))):
        return subject
    return None


def program_overview_subject(question):
    """Treat only a bare degree title/code as an overview, not an arbitrary topic."""
    text = request_text(question)
    if re.search(r"\b(?:fees?|tuition|requirements?|curriculum|subjects?|credits?|deans?|objectives?|admission|enrollment)\b",
                 text, re.IGNORECASE):
        return None
    degree = re.match(r"^(?:bachelor|master|doctor|associate)\b", text, re.IGNORECASE)
    code = re.fullmatch(r"[bmad][a-z]{2,11}", text, re.IGNORECASE) and is_known_acronym(text)
    field = re.fullmatch(r"(?:ba|bs|ma)\s+[a-z][a-z -]{1,60}", text, re.IGNORECASE)
    return program_dean_subject(f"dean of {text}") if degree or code or field else None


def _program_title(value):
    value = re.split(r"\b(?:CMO|PSG|CHED|implementing)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.sub(r"\s*\([^)]*\)", "", value)
    value = re.sub(r"\s+(?:majors?\s+in|with\s+specializations?\s+in)\s*:\s*$", "", value, flags=re.IGNORECASE)
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def program_affiliations(subject, documents, *, alias_documents=None, include_alias_evidence=False):
    """Find exact degree/acronym rows under a stated parent, never mere co-occurrence."""
    target = _program_title(subject or "")
    if not target:
        return {}
    documents = list(documents)
    compact_target = target.replace(" ", "")
    shorthand_codes = program_code_candidates(target)
    candidate_codes = {compact_target, *shorthand_codes}
    degree = re.compile(r"^(?:bachelor|master|doctor|associate)\b", re.IGNORECASE)
    excluded = re.compile(r"\b(?:staff|faculty|biograph(?:y|ies)|qualifications?|alumni|academic council|deans? update)\b",
                          re.IGNORECASE)
    definition = re.compile(
        r"(\b(?:bachelor|master|doctor|associate)\b"
        r"(?:(?!\b(?:bachelor|master|doctor|associate)\b)[^\n|]){1,220}?)"
        r"\s*\(([a-z0-9 .-]{2,24})\)", re.IGNORECASE,
    )
    labels = {target: []}
    # Degree definitions can supply the acronym missing from a separate catalog.
    # They establish the label only; a named catalog parent is still required.
    for document in documents if alias_documents is None else alias_documents:
        metadata = getattr(document, "metadata", None) or {}
        headings = "\n".join(str(metadata.get(key) or "") for key in _HEADINGS)
        if excluded.search(headings):
            continue
        text = headings + "\n" + (getattr(document, "page_content", "") or "")
        for match in definition.finditer(text):
            if re.sub(r"[ .-]", "", match[2]).casefold() in candidate_codes:
                title = _program_title(match[1])
                if title and not any(existing[0] is document for existing in labels.setdefault(title, [])):
                    labels[title].append((document, match[0]))
    affiliations = {}
    matched_titles = set()
    for document in documents:
        metadata = getattr(document, "metadata", None) or {}
        headings = "\n".join(str(metadata.get(key) or "") for key in _HEADINGS)
        if excluded.search(headings):
            continue
        text = getattr(document, "page_content", "") or ""
        text = re.sub(r"\[(?:SECTION|TABLE context):\s*([^\]]+)\]", r"\n\1\n", text, flags=re.IGNORECASE)
        lines = [re.sub(r"^\s*(?:[-*#]+|[o\u2022]\s+|\d+[.)]\s*)", "", line).strip()
                 for line in re.split(r"\n|\|", text) if line.strip()]
        parents = list(dict.fromkeys(name for name, _, _ in _names(headings)))
        parent = parents[0] if len(parents) == 1 else None
        if parent:
            # A curriculum chunk can carry the program title in its section header.
            lines = [*(str(metadata.get(key) or "") for key in _HEADINGS
                       if degree.match(str(metadata.get(key) or ""))), *lines]
        index = 0
        while index < len(lines):
            line = lines[index]
            named = _names(line)
            if _HEAD.match(line) and len(named) == 1 and not re.search(
                r"\b(?:is|are|was|were|offers?|provides?|has|have|attended|graduated)\b", line, re.IGNORECASE,
            ):
                parent = named[0][0]
                index += 1
                continue
            if not degree.match(line) and (_ROSTER_HEADING.search(line) or re.match(_ROLE_PREFIX, line, re.IGNORECASE)):
                parent = None
            if parent and degree.match(line):
                direct_normalized = _program_title(line)
                row = line
                following = index + 1
                while following < len(lines) and following <= index + 2 and not re.search(r"\([a-z0-9 .-]{2,16}\)", row,
                                                                                        re.IGNORECASE):
                    if _HEAD.match(lines[following]) or degree.match(lines[following]) or _ROSTER_HEADING.search(lines[following]):
                        break
                    row += " " + lines[following]
                    following += 1
                codes = [re.sub(r"[ .-]", "", code).casefold() for code in re.findall(r"\(([a-z0-9 .-]{2,24})\)", row,
                                                                                  re.IGNORECASE)]
                titles = (_program_title(row), direct_normalized)
                label = target if candidate_codes.intersection(codes) else next((name for name in labels if any(
                    title == name or re.match(rf"^{re.escape(name)}\s+(?:majors?\s+in|with\s+specializations?\s+in)\b", title)
                    for title in titles
                )), None)
                if label is not None:
                    if shorthand_codes:
                        matched_titles.add(titles[0])
                    key = next((name for name in affiliations if same_unit(name, parent)), parent)
                    proofs = [document]
                    if include_alias_evidence:
                        for alias_document, declaration in labels[label][:1]:
                            proof = copy(alias_document)
                            proof.page_content = declaration
                            proof.metadata = {key: value for key, value in (alias_document.metadata or {}).items()
                                              if key not in _HEADINGS}
                            proof.metadata["section_heading"] = "Program acronym definition"
                            proofs.append(proof)
                    for proof in proofs:
                        if not any(existing is proof for existing in affiliations.setdefault(key, [])):
                            affiliations[key].append(proof)
            index += 1
    if shorthand_codes and len(matched_titles) > 1:
        return {}
    # A genuine college's own acronym takes precedence over a coincidental degree code.
    if re.fullmatch(r"[a-z]{2,12}", target) and any(
        target == "".join(word[0] for word in _tokens(name)) or same_unit(target, name)
        for document in documents for name in document_unit_names(document)
    ):
        return {}
    return affiliations
