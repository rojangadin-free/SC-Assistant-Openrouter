"""
rag/escalation.py — give the student a next step when the documents can't answer.

Why this exists
---------------
`rag/gaps.py` fixed the *admin* half of an unanswered question: the topic gets
logged and ranked so the right document eventually gets uploaded. But the student
in front of the screen right now still gets a dead end — and "eventually" does not
help someone who needs to enroll this week.

This module closes the student half. When an answer comes back unanswered, the
question can be **escalated to a human**: it is queued for the admin with the
student's contact details, and the admin replies. Same question, two outcomes:
the corpus improves *and* this particular student gets an answer.

    student asks -> no grounded answer
        -> gap logged            (fix the documents)
        -> "Ask the registrar"   (fix it for THIS student)   <- this module

How it differs from a report
----------------------------
A report says "this answer was wrong". An escalation says "nobody has answered me
yet". They need different queues because they need different actions: a report is
reviewed, an escalation is *replied to*. Merging them would bury real questions
under quality complaints.

Design notes
------------
* **Escalations are grouped by topic** using `rag.gaps.normalize_question`, the
  same normalizer the gap queue uses, so an admin looking at "12 students asked
  about the shuttle" sees the same 12 in both screens.

* **Guests can escalate only if they leave contact details.** An escalation with
  no way to reply is not a request, it is litter — so `contact` is required when
  there is no logged-in email.

* **Answering an escalation is recorded, not just sent.** The reply text is kept
  on the row, which means the admin can paste it into a document later, and the
  same answer is available if another student asks the same thing.

* **The student can read the reply in the app.** Storing the answer without
  showing it to the person who asked would leave the loop open: the modal promises
  "they will reply", so the reply has to be somewhere the student can find. See
  `my_escalations()` / `mark_escalation_read()` and `/chat/escalations/mine`.
"""


from __future__ import annotations

import datetime
import os
import re
import threading
import uuid
from typing import List, Optional

from rag.gaps import normalize_question
from rag.store import JsonBlobStore


ESCALATIONS_FILE = os.getenv("ESCALATIONS_FILE", "escalations.json")

_lock = threading.Lock()

MAX_ESCALATIONS = 1000

STATUSES = ("pending", "answered", "dismissed")

# Where a question can be sent. Kept as data (not hardcoded in the UI) so adding
# an office later is a one-line change and the admin queue can filter by it.
ROUTES = {
    "registrar": "Registrar's Office",
    "cashier": "Cashier / Accounting",
    "admissions": "Admissions Office",
    "guidance": "Guidance Office",
    "it": "IT / SCTI",
    "admin": "College Administration",
}
DEFAULT_ROUTE = "admin"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE = re.compile(r"^\+?[\d\s\-()]{7,20}$")


def valid_contact(contact: str) -> bool:
    """
    True if `contact` is something a human could actually reply to.

    Deliberately permissive about format (Philippine mobile numbers get written a
    dozen ways) but strict about *having* one, because an escalation nobody can
    answer wastes the admin's time and the student's hope.
    """
    c = (contact or "").strip()
    if not c:
        return False
    return bool(_EMAIL.match(c) or _PHONE.match(c))


# ============================================================
# STORAGE
# ============================================================

def _empty_store() -> dict:
    return {"items": {}, "updated_at": ""}


# Delegated to `rag.store` (file by default, DynamoDB when STORE_BACKEND=dynamodb).
# This is the store where per-instance state is most obviously indefensible: a
# student files a question on instance A, the admin opens the queue on instance B
# and sees an empty list, so a real person waiting for a reply is never answered
# and nobody knows anything was lost.
_store_obj = None
_store_obj_path = None
_store_init_lock = threading.Lock()


def _store():
    global _store_obj, _store_obj_path
    path = os.getenv("ESCALATIONS_FILE", ESCALATIONS_FILE)
    with _store_init_lock:
        if _store_obj is None or _store_obj_path != path:
            _store_obj = JsonBlobStore("escalations", path, _empty_store)
            _store_obj_path = path
        return _store_obj


def _load() -> dict:
    data = _store().load()
    return data if "items" in data else _empty_store()


def _save(store: dict) -> None:
    _store().save(store)



def _prune(items: dict) -> dict:
    """Bounded file; answered/dismissed rows go first, then oldest."""
    if len(items) <= MAX_ESCALATIONS:
        return items
    ordered = sorted(
        items.items(),
        key=lambda kv: (
            kv[1].get("status") != "pending",
            kv[1].get("created_at", ""),
        ),
        reverse=True,
    )
    return dict(ordered[:MAX_ESCALATIONS])


# ============================================================
# PUBLIC API
# ============================================================

def create_escalation(
    question: str,
    *,
    route: str = DEFAULT_ROUTE,
    student_email: str = "",
    contact: str = "",
    note: str = "",
    conv_id: str = "",
    asked_answer: str = "",
) -> Optional[dict]:
    """
    Queue one question for a human. Returns the row, or None if it is unusable.

    A reachable contact is required: `student_email` (from the session) counts,
    otherwise `contact` must be supplied and valid. That is what separates this
    from the gap log, which happily records anonymous questions because nobody
    needs to reply to those.
    """
    q = " ".join((question or "").split())
    if not q:
        return None

    reply_to = (student_email or "").strip() or (contact or "").strip()
    if not reply_to or (not student_email and not valid_contact(contact)):
        return None

    if route not in ROUTES:
        route = DEFAULT_ROUTE

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    esc_id = str(uuid.uuid4())

    try:
        with _lock:
            store = _load()
            items = store.setdefault("items", {})
            items[esc_id] = {
                "id": esc_id,
                "question": q[:500],
                "topic_key": normalize_question(q),
                "route": route,
                "route_label": ROUTES[route],
                "student_email": (student_email or "").strip(),
                "contact": (contact or "").strip(),
                "reply_to": reply_to,
                "note": " ".join((note or "").split())[:500],
                "bot_answer": " ".join((asked_answer or "").split())[:300],
                "conv_id": conv_id,
                "status": "pending",
                "reply": "",
                "answered_by": "",
                "answered_at": "",
                # Whether the student has opened the reply. Tracked per row rather
                # than as a global "last seen" timestamp so a student with two
                # escalations can read one and still be badged for the other.
                "read_at": "",
                "created_at": now,
            }
            store["items"] = _prune(items)
            _save(store)

            return store["items"].get(esc_id)
    except Exception as e:
        print(f"  [escalation] could not create (non-fatal): {e}")
        return None


def list_escalations(status: Optional[str] = None, route: Optional[str] = None) -> List[dict]:
    """
    Queue contents, **oldest pending first** — this is a to-do list, and the
    student who has waited longest should be answered first. (Note this is the
    opposite of the gap queue, which ranks by demand; here every row is one real
    person waiting for a reply.)
    """
    items = list(_load().get("items", {}).values())
    if status:
        items = [i for i in items if i.get("status") == status]
    if route:
        items = [i for i in items if i.get("route") == route]
    items.sort(key=lambda i: (i.get("status") != "pending", i.get("created_at", "")))
    return items


def escalation_stats() -> dict:
    items = list(_load().get("items", {}).values())
    pending = [i for i in items if i.get("status") == "pending"]
    by_route = {}
    for i in pending:
        by_route[i.get("route", "")] = by_route.get(i.get("route", ""), 0) + 1
    return {
        "total": len(items),
        "pending": len(pending),
        "answered": sum(1 for i in items if i.get("status") == "answered"),
        "dismissed": sum(1 for i in items if i.get("status") == "dismissed"),
        "pending_by_route": by_route,
    }


def answer_escalation(esc_id: str, reply: str, *, answered_by: str = "") -> Optional[dict]:
    """
    Record the human reply.

    The text is stored rather than only emailed, because the reply *is* content:
    it is the raw material for the document that should have answered this in the
    first place, and the next student asking the same thing benefits from it.
    """
    reply = " ".join((reply or "").split())
    if not reply:
        return None
    with _lock:
        store = _load()
        item = store.get("items", {}).get((esc_id or "").strip())
        if not item:
            return None
        item["status"] = "answered"
        item["reply"] = reply[:2000]
        item["answered_by"] = answered_by
        item["answered_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # A new reply is unread by definition. Clearing this here (rather than only
        # on create) means an edited or re-sent answer re-notifies the student,
        # which is the behaviour they would expect from a message.
        item["read_at"] = ""
        _save(store)
        return item


# ============================================================
# THE STUDENT'S SIDE OF THE QUEUE
# ============================================================
# Everything above serves the admin. These two functions serve the person who
# asked: without them the reply exists in storage and nowhere the student can
# reach, and the promise in the escalate modal ("they will reply") is not kept by
# the app itself.

def my_escalations(*, student_email: str = "", contact: str = "") -> List[dict]:
    """
    Every escalation belonging to one person, newest first, with admin-only fields
    removed.

    Identity is `reply_to` — the address the escalation was filed under. It is the
    only handle that works for both cases: a signed-in student is matched on their
    account email, a guest on the contact they typed. Matching is case-insensitive
    because email is, and because a guest who types `Me@Gmail.com` today and
    `me@gmail.com` tomorrow is the same person.

    Dismissed rows are withheld. "Dismissed" is an internal judgement (spam, a
    duplicate, a question already answered elsewhere); surfacing it would put the
    admin in the position of visibly rejecting a student with no explanation
    attached, which is worse than silence.
    """
    ident = {
        (student_email or "").strip().lower(),
        (contact or "").strip().lower(),
    } - {""}
    if not ident:
        return []

    out = []
    for item in _load().get("items", {}).values():
        if item.get("status") == "dismissed":
            continue
        who = (item.get("reply_to") or "").strip().lower()
        if who and who in ident:
            out.append({
                "id": item.get("id", ""),
                "question": item.get("question", ""),
                "route_label": item.get("route_label", ""),
                "status": item.get("status", "pending"),
                "reply": item.get("reply", ""),
                # The admin's identity is deliberately reduced to the office name:
                # a reply from "the Registrar's Office" is authoritative, while a
                # reply from a named staff account invites students to contact
                # that person directly and bypass the queue.
                "answered_at": item.get("answered_at", ""),
                "created_at": item.get("created_at", ""),
                "unread": bool(item.get("reply")) and not item.get("read_at"),
            })

    out.sort(key=lambda i: i.get("created_at", ""), reverse=True)
    return out


def mark_escalation_read(esc_id: str, *, student_email: str = "", contact: str = "") -> bool:
    """
    Clear the unread badge for one row.

    The caller's identity is re-checked against `reply_to` rather than trusted:
    the id is a UUID, but ids travel in URLs and logs, and marking someone else's
    reply as read would silently hide it from them.
    """
    ident = {
        (student_email or "").strip().lower(),
        (contact or "").strip().lower(),
    } - {""}
    if not ident:
        return False

    with _lock:
        store = _load()
        item = store.get("items", {}).get((esc_id or "").strip())
        if not item:
            return False
        if (item.get("reply_to") or "").strip().lower() not in ident:
            return False
        if item.get("read_at"):
            return True
        item["read_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _save(store)
        return True


def set_status(esc_id: str, status: str) -> Optional[dict]:
    """Dismiss spam, or push a row back into the queue."""
    if status not in STATUSES:
        return None
    with _lock:
        store = _load()
        item = store.get("items", {}).get((esc_id or "").strip())
        if not item:
            return None
        item["status"] = status
        if status == "pending":
            item["reply"] = ""
            item["answered_by"] = ""
            item["answered_at"] = ""

        _save(store)
        return item


def delete_escalation(esc_id: str) -> bool:
    with _lock:
        store = _load()
        if (esc_id or "").strip() in store.get("items", {}):
            del store["items"][esc_id.strip()]
            _save(store)
            return True
    return False
