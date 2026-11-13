"""Fixed-topic usage counts, never a question log or a permanent event ledger."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime, timedelta, timezone

from rag.calendar import MANILA
from rag.store import JsonBlobStore

TOPIC_LABELS: dict[str, str] = {
    "enrollment": "Admissions and enrollment",
    "fees": "Tuition and payments",
    "scholarships": "Scholarships",
    "programs": "Programs and courses",
    "calendar": "Schedules and deadlines",
    "offices": "Offices and contacts",
    "records": "Student records",
    "policies": "School policies",
    "facilities": "Campus services",
    "activities": "Student activities",
    "accounts": "Account access",
    "other": "Other questions",
}
TOPIC_QUESTIONS = {
    "enrollment": "How do I enroll?",
    "programs": "What programs does Samar College offer?",
    "fees": "What are the tuition fees and payment options?",
    "scholarships": "What scholarships are available?",
    "calendar": "What are the important dates in the academic calendar?",
    "offices": "Who are the college deans and school officials?",
    "records": "How do I request my school records?",
    "policies": "What school policies should I know?",
    "facilities": "What library and campus facilities are available?",
    "activities": "What student clubs and activities can I join?",
    "accounts": "How do I reset my account password?",
}
DEFAULT_SUGGESTIONS = tuple(TOPIC_QUESTIONS[key] for key in ("enrollment", "programs", "fees"))
MAX_RECENT_EVENTS = 512
_TOPIC_KEYS = frozenset(TOPIC_LABELS)
_MAX_COUNT = (1 << 63) - 1
_EVENT_DOMAIN = b"sc-assistant:topic-usage:v1\0"
_HASH = re.compile(r"[0-9a-f]{64}")

# Specific subjects precede broader school terms: tuition for a course is fees,
# a TOR from the registrar is records, and a dean of a program is an office.
_RULES = tuple((key, re.compile(r"\b(?:" + pattern + r")\b")) for key, pattern in (
    ("accounts", (r"passwords?|passcodes?|log ?in|sign ?in|accounts?|"
                  r"(?:maka|mag|pag)login|verification code|one time password|otp")),
    ("scholarships", (r"scholarships?|scholars?|iskolar|iskolars|financial aid|"
                      r"student grants?|tulong pinansyal")),
    ("fees", (r"tuition|fees?|payments?|installments?|instalments?|refunds?|"
              r"matrikula|bayad|bayarin|magbayad|pagbabayad")),
    ("records", (r"transcripts?|tor|grades?|report cards?|form (?:137|138)|"
                 r"(?:student|school) (?:records?|id)|id cards?|diplomas?|"
                 r"certificates? of (?:enrollment|enrolment|registration|good moral)|"
                 r"good moral|grado|marka")),
    ("offices", (r"deans?|dekano|registrar|cashier|offices?|tanggapan|"
                 r"(?:college|school) president|contacts?|hotline|phone number|"
                 r"telephone|telepono")),
    ("policies", (r"polic(?:y|ies)|handbook|rules?|regulations?|dress code|"
                  r"uniforms?|attendance|disciplin(?:e|ary)|patakaran|alituntunin")),
    ("activities", (r"clubs?|organizations?|organisations?|student council|"
                   r"student activities|intramurals?|sports? fest|festivals?|"
                   r"organisasyon|samahan")),
    ("facilities", (r"librar(?:y|ies)|clinic|canteen|cafeteria|dormitor(?:y|ies)|"
                   r"dorms?|parking|shuttle|wifi|wi fi|laborator(?:y|ies)|"
                   r"guidance|counseling|counselling|campus map|gym|"
                   r"aklatan|paradahan")),
    ("programs", (r"programs?|courses?|curricul(?:um|a)|degrees?|majors?|"
                 r"subjects?|bachelor|bsit|bsba|bsed|beed|bsn|bs nursing|"
                 r"computer science|information technology|kurso|kursong")),
    ("enrollment", (r"enroll?(?:ment|ing|ed)?|admissions?|entrance exam|"
                   r"transferees?|freshm[ae]n|pag enroll?|mag enroll?|"
                   r"magenroll?|pagenroll?|pagpapatala")),
    ("calendar", (r"schedules?|deadlines?|calendar|academic year|holidays?|"
                 r"class(?:es)? (?:start|begin|resume)|start of classes|"
                 r"exam dates?|pasukan|iskedyul|takdang araw")),
))
_SMALL_TALK = re.compile(
    r"(?:(?:hi|hello(?: there)?|hey|good (?:morning|afternoon|evening|day)|"
    r"magandang (?:umaga|hapon|gabi|araw)|"
    r"thank you(?: very much| so much| for (?:your |the )?help)?|"
    r"thanks(?: a lot| so much| for (?:your |the )?help)?|"
    r"(?:maraming )?salamat(?: sa tulong)?|ok|okay|sige|noted|got it|"
    r"understood|alright|yes|no|that helps|po|opo|sir|maam|ma am|please) ?)+"
)
_FOLLOWUP = re.compile(
    r"(?:and |also |then )?(?:"
    r"(?:what |how )?about (?:it|that|this|those|(?:the )?(?:requirements|process|steps))|"
    r"(?:what|which) (?:are|is) (?:the |its |their )?"
    r"(?:requirements|documents|steps|process|details|qualifications)|"
    r"(?:what|which) (?:else|ones?)|"
    r"(?:the |more )?(?:requirements?|documents?|steps?|process|details?|eligibility)|"
    r"(?:how|where|when) (?:do|can|should) i "
    r"(?:apply|submit (?:it|them|these|those)|do (?:it|that)|get (?:it|them))|"
    r"how (?:much|long)(?: (?:is|does|will) (?:it|that|this)(?: cost| take)?)?|"
    r"(?:what|when|where) (?:is|are) (?:it|that|this|those|them)|"
    r"(?:can you )?(?:explain|clarify|elaborate)(?: it|that|this| more| further)?|"
    r"tell me more|does (?:it|that|this) apply to me|when|where|how|"
    r"(?:ano|anu ano|ano ano) (?:ang |mga |ang mga )?(?:kailangan|requirements|hakbang)|"
    r"(?:paano|saan|kailan)(?: ito| iyan| iyon| yun| yan| naman)?|"
    r"magkano(?: ito| iyan| iyon| yun| yan)?"
    r")"
)


def _text(value) -> str:
    if not isinstance(value, str):
        return ""
    # Email addresses and URLs are not subject words. Nothing here is persisted.
    value = re.sub(r"https?://\S+|\b[\w.+-]+@[\w.-]+", " ", value.casefold())
    return " ".join(re.findall(r"[^\W_]+", value))


def _explicit_topic(text: str) -> str | None:
    for key, rule in _RULES:
        if rule.search(text):
            return key
    return None


def _is_followup(text: str) -> bool:
    words = [word for word in text.split() if word not in {"po", "please"}]
    return len(words) <= 12 and _FOLLOWUP.fullmatch(" ".join(words)) is not None


def classify_topic(question, *, history=None, has_image=False) -> str | None:
    """Classify transient text; only short, explicit follow-ups may use history."""
    text = _text(question)
    if not text:
        return "other" if has_image else None
    if _SMALL_TALK.fullmatch(text):
        return None
    topic = _explicit_topic(text)
    if topic is not None:
        return topic
    if has_image:
        return "other"
    if _is_followup(text) and isinstance(history, (list, tuple)):
        for message in reversed(history[-6:]):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            recent = _text(message.get("content"))
            topic = _explicit_topic(recent)
            if topic is not None:
                return topic
            # An unrelated question or an uncaptioned image ends the subject.
            if not recent or not (_SMALL_TALK.fullmatch(recent) or _is_followup(recent)):
                break
    return "other"


def _store(month: str) -> JsonBlobStore:
    directory = os.getenv("TOPIC_USAGE_DIR") or "topic_usage"
    return JsonBlobStore(
        "topic_usage:" + month, os.path.join(directory, month + ".json"),
        lambda: {"days": {}, "recent_events": []},
    )


def _validate(data: dict, month: str) -> None:
    """Reject corruption rather than displaying or overwriting invented totals."""
    if (not {"days", "recent_events"}.issubset(data)
            or set(data) - {"days", "recent_events", "updated_at"}):
        raise ValueError("Invalid topic usage schema.")
    days, recent = data["days"], data["recent_events"]
    if not isinstance(days, dict) or len(days) > 31:
        raise ValueError("Invalid topic usage days.")
    for day, counts in days.items():
        try:
            valid_day = isinstance(day, str) and date.fromisoformat(day).isoformat() == day
        except ValueError:
            valid_day = False
        if not valid_day or day[:7] != month:
            raise ValueError("Invalid topic usage day.")
        if (not isinstance(counts, dict) or set(counts) - _TOPIC_KEYS
                or any(type(count) is not int or not 0 <= count <= _MAX_COUNT
                       for count in counts.values())):
            raise ValueError("Invalid topic usage counters.")
    if (not isinstance(recent, list) or len(recent) > MAX_RECENT_EVENTS
            or any(not isinstance(event, str) or _HASH.fullmatch(event) is None for event in recent)
            or len(set(recent)) != len(recent)):
        raise ValueError("Invalid topic usage retry ring.")
    if "updated_at" in data:
        stamp = data["updated_at"]
        if not isinstance(stamp, str) or len(stamp) > 40:
            raise ValueError("Invalid topic usage metadata.")
        try:
            updated_at = datetime.fromisoformat(stamp)
        except ValueError:
            raise ValueError("Invalid topic usage metadata.") from None
        if updated_at.utcoffset() != timedelta(0):
            raise ValueError("Invalid topic usage metadata.")


def record_topic_usage(topic, event_id, *, at: datetime | None = None) -> bool:
    """Atomically increment once per recent event in its Manila monthly shard.

    Retry deduplication lasts only while the hash is in that month's 512-entry
    ring. Historical counts remain; this is not an indefinite replay ledger.
    """
    if topic is None:
        return False
    if not isinstance(topic, str) or topic not in _TOPIC_KEYS:
        raise ValueError("Unknown topic category.")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("A nonblank event ID is required.")
    if at is not None and (not isinstance(at, datetime) or at.utcoffset() is None):
        raise ValueError("An aware recording datetime is required.")
    # Capture both bucket and hash once; mutate may retry across midnight.
    instant = at if at is not None else datetime.now(timezone.utc)
    day = instant.astimezone(MANILA).date().isoformat()
    month = day[:7]
    event_hash = hashlib.sha256(_EVENT_DOMAIN + event_id.encode("utf-8")).hexdigest()

    def change(data):
        _validate(data, month)
        recent = data["recent_events"]
        if event_hash in recent:
            return False
        counts = data["days"].setdefault(day, {})
        count = counts.get(topic, 0)
        if count == _MAX_COUNT:
            raise OverflowError("Topic counter is full.")
        counts[topic] = count + 1
        recent.append(event_hash)
        del recent[:-MAX_RECENT_EVENTS]
        return True

    return _store(month).mutate(change, return_result=True)


def most_asked_topics(limit=10, *, days=14, today=None) -> list[dict]:
    """Read an inclusive Manila day window; missing data is empty, errors raise."""
    limit = max(1, min(100, int(limit)))
    days = max(1, min(365, int(days)))
    if today is None:
        today = datetime.now(timezone.utc).astimezone(MANILA).date()
    elif isinstance(today, datetime):
        if today.utcoffset() is None:
            raise ValueError("An aware query datetime is required.")
        today = today.astimezone(MANILA).date()
    elif not isinstance(today, date):
        raise ValueError("A query date is required.")
    start = today - timedelta(days=days - 1)
    months = sorted({(start + timedelta(days=i)).isoformat()[:7] for i in range(days)})
    first, last = start.isoformat(), today.isoformat()
    totals = {}
    for month in months:
        # load() masks failures. mutate(False) is a strict snapshot with no save/Put.
        data = _store(month).mutate(lambda data: False)
        _validate(data, month)
        for day, counts in data["days"].items():
            if first <= day <= last:
                for key, count in counts.items():
                    totals[key] = totals.get(key, 0) + count
    rows = [{"key": key, "topic": TOPIC_LABELS[key], "total": total}
            for key, total in totals.items() if total]
    rows.sort(key=lambda row: (-row["total"], row["topic"]))
    return rows[:limit]


def suggested_questions() -> list[str]:
    """Project the default topic window into three public, curated questions."""
    try:
        ranked = most_asked_topics(len(TOPIC_LABELS))
    except Exception:  # noqa: BLE001 - suggestions must not depend on store availability
        return list(DEFAULT_SUGGESTIONS)
    questions = []
    for row in ranked or []:
        key = row.get("key") if isinstance(row, dict) else None
        question = TOPIC_QUESTIONS.get(key) if isinstance(key, str) else None
        if question and question not in questions:
            questions.append(question)
        if len(questions) == 3:
            return questions
    for question in DEFAULT_SUGGESTIONS:
        if question not in questions:
            questions.append(question)
        if len(questions) == 3:
            break
    return questions
