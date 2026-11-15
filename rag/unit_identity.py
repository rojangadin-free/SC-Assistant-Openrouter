"""Conservative, single-subject unit identity checks without a school-name table."""

import re
import unicodedata
from dataclasses import dataclass

from rag.spelling import is_known_acronym

_HEAD = re.compile(
    r"\b(?:college|school|department|faculty|institute|office|center|centre|university)\s+(?:of|for)\s+",
    re.IGNORECASE,
)
_TAIL = re.compile(
    r"\b(?:offers?|offered|provides?|includes?|has|have|is|are|was|were|does|do|did|who|what|where|"
    r"when|why|how|which)\b|"
    r"\b(?:in|during|as of|from|for(?:\s+(?:the\s+)?(?:school|academic)\s+year)?)\s+20\d{2}\b", re.IGNORECASE,
)
_CONNECTORS = {"of", "for", "and", "the", "a", "an"}
_GENERIC = {"it", "me", "us", "we", "my", "what", "which", "who", "where", "when", "how", "why",
            "college", "school", "campus", "office", "faculty", "unit", "dean", "head", "staff",
            "fee", "fees", "cost", "help", "program", "programs", "course", "courses", "gpa", "gwa", "id",
            "in", "on", "at", "by", "from", "with", "to", "than", "under", "art", "arts", "name",
            "current", "new", "old", "latest", "history", "academic", "for", "of", "and", "or", "the", "a", "an"}
_GENERIC |= {word + "s" for word in _GENERIC}
_HEADINGS = ("collection_heading", "section_heading", "section", "header")
_ROLE_PREFIX = (
    r"(?:the\s+)?(?:(?:oic|acting|interim|assistant|associate)[\s-]+)*"
    r"(?:deans?|heads?|chairs?|chairpersons?|principals?|directors?|registrars?)"
    r"(?:\s+(?:of|for|at)\s+|\s*[,:-]\s*|\s+(?=(?:college|school|department|faculty)\b))"
)
_ROSTER_HEADING = re.compile(
    r"\b(?:academic\s+council|personnel|staff|faculty\s+roster|leadership|administration|officials?|"
    r"organizational\s+structure)\b", re.IGNORECASE,
)


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
    heads = list(_HEAD.finditer(text))
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
        name = text[head.start():end].strip(" ,\t")
        name = re.sub(r"(?:\s+(?:and|or|the|a|an))+$", "", name, flags=re.IGNORECASE)
        if len(_tokens(name)) >= 2 and len(name) <= 180:
            result.append((name, head.start(), head.start() + len(name)))
    return result


def requested_unit(question):
    """Only bind a single explicit subject; do not collapse compound requests."""
    question = (question or "").strip()
    names = _names(question)
    if len(names) == 1:
        name, start, end = names[0]
        remainder = question[:start] + " " + question[end:]
        if not re.search(r"\b(?:and|or|versus|vs)\b|;", remainder, re.IGNORECASE):
            return UnitRequest(name, remainder)
    if names:
        return None
    # Bare short identifiers are tentative until the retrieved units establish
    # an actual initialism. This does not turn ordinary words into departments.
    bare = re.fullmatch(r"\s*(?:(?:i mean|tell me about|what is|what about|about)\s+)?([a-z]{2,8})[?.!]*", question, re.IGNORECASE)
    if bare and bare[1].casefold() not in _GENERIC:
        return UnitRequest(bare[1], "", False)
    for pattern, subject in (
        (r"\b(?:deans?|heads?|chairs?)\s+(?:of|for|at)\s+([a-z]{2,8})\b", True),
        (r"\b(?:departments?|colleges?|units?)\s+([a-z]{2,8})\b", False),
        (r"\b(?:programs?|courses?)\s+(?:does|do)\s+([a-z]{2,8})\s+(?:offer|have)\b", True),
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
    rows = [line.strip().lstrip(" #*>\t-0123456789.)") for line in re.split(r"\n|\|", text)]
    if any(_ROSTER_HEADING.search(heading) for heading in headings):
        # Indexed roster rows can place a name and multiple roles on one line.
        # This extracts affiliations for identity matching only; names and role
        # pairings remain in the unchanged source text used to answer.
        affiliations = [
            re.sub(r"\s+Member\s*$", "", line[match.start():], flags=re.IGNORECASE)
            for line in rows for match in re.finditer(_ROLE_PREFIX, line, re.IGNORECASE)
        ]
        rows.extend(affiliations)
    # Roster designations commonly use "Dean, College of ...", not "Dean of ...".
    # Retain the original document so the adjoining name stays paired with its role.
    rows = [line for line in rows if _HEAD.match(line) or re.match(_ROLE_PREFIX, line, re.IGNORECASE)]
    return [*headings, *rows]


def document_unit_names(document):
    return tuple(name for value in _sections(document) for name, _, _ in _names(value))


def document_matches_unit(document, request):
    """Use named sections/rows, not word overlap or an incidental mention."""
    names = document_unit_names(document)
    if _HEAD.match(request.name):
        return any(same_unit(request.name, name) for name in names)
    code = request.name.casefold()
    if request.protected and any("".join(word[0] for word in _tokens(name)) == code for name in names):
        return True
    if request.explicit:
        subject = re.compile(
            rf"^(?:{_ROLE_PREFIX})?{re.escape(request.name)}"
            r"(?:\s*$|\s*[:(]|\s+(?:department|college|program|staff|faculty|curriculum)\b)", re.IGNORECASE,
        )
        return any(subject.match(line.strip()) for line in _sections(document))
    return False
