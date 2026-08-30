"""
rag/roles.py — who is asking, and what that changes about the answer.

The bug this closes
-------------------
`ChatState` has carried an `is_faculty` flag since the announcements work, and
`rag/announcements.py` already routes faculty-only notices by it. Nothing ever
set it. Every request reached the graph without the key, `state.get("is_faculty")`
returned `None`, and so **every** user — including the registrar and the deans —
was served the student view. A faculty-targeted announcement was therefore
deliverable in theory and undeliverable in practice.

That is the concrete half. The broader half is that "how do I enroll?" is not one
question:

  a student asks  -> the steps THEY perform, at the windows they queue at
  a teacher asks  -> what they must do for their advisees, and by when

Answering a teacher with the student checklist is not wrong, exactly. It is
useless, which in a support tool is the same thing.

What this module is NOT
-----------------------
It is **not** an access-control boundary. Role here shapes *emphasis and
audience*, and the only thing it withholds is the faculty announcement feed
(which was already scoped that way). Personal records stay gated by `uid` +
`data_consent` in `rag/chain.py`, exactly as before: a teacher asking about
"my grades" still gets their own record and nobody else's, because the record is
fetched by their own uid and no prompt text can widen that.

Stating this in the file matters, because a module named `roles.py` invites the
assumption that it is doing authorization. If a future change wants faculty-only
*documents*, that belongs in retrieval with a metadata filter — not in a prompt
instruction, which is advisory and can be talked out of.

Design notes
------------
- Roles come from the session, which comes from a Cognito `custom:role` claim.
  Unknown values fall back to `student`, because the student view is the one that
  is safe to show the wrong person.
- `guest` is distinct from `student`. A guest has no record and no enrolment, so
  the useful default for them is the prospective-student view, and the prompt
  says so instead of leaving the model to infer it from an absent record.
- The block is short by design. Long role preambles compete with `<verified_facts>`
  and `<announcements>` for the model's attention, and those two carry facts while
  this one only carries framing.
"""

from __future__ import annotations

from typing import Optional

# The canonical audiences. Deliberately small: every value here has to be
# meaningfully different in the prompt, or it is a synonym pretending to be a
# feature.
STUDENT = "student"
FACULTY = "faculty"
GUEST = "guest"

ROLES = (GUEST, STUDENT, FACULTY)

# Session/Cognito values seen in the wild, mapped onto the three above.
#
# "user" is Cognito's default for anyone who signed up without a custom:role, so
# it MUST mean student — treating an unlabelled account as faculty would hand the
# faculty announcement feed to the general population.
_ALIASES = {
    "": STUDENT,
    "user": STUDENT,
    "student": STUDENT,
    "students": STUDENT,
    "guest": GUEST,
    "visitor": GUEST,
    "faculty": FACULTY,
    "teacher": FACULTY,
    "instructor": FACULTY,
    "professor": FACULTY,
    "staff": FACULTY,
    "registrar": FACULTY,
    "dean": FACULTY,
    "admin": FACULTY,        # an admin asking the chatbot is asking as staff
    "administrator": FACULTY,
}

# Human labels, for the prompt and for admin screens.
LABELS = {
    GUEST: "Prospective student / visitor",
    STUDENT: "Enrolled student",
    FACULTY: "Faculty or staff member",
}


def resolve_role(session_role: Optional[str] = None,
                 *,
                 is_guest: bool = False) -> str:
    """
    Normalise whatever the session holds into one of `ROLES`.

    `is_guest` wins over the role string. The guest login writes BOTH
    `role="guest"` and `is_guest=True`, but a stale session from before that
    change can carry `role="user"` with `is_guest=True`, and in that pairing the
    guest flag is the one that reflects reality: there is no uid behind it.
    """
    if is_guest:
        return GUEST

    key = (session_role or "").strip().lower()
    return _ALIASES.get(key, STUDENT)


def is_faculty(session_role: Optional[str] = None,
               *,
               is_guest: bool = False) -> bool:
    """
    Convenience for the one caller that needs a boolean: the announcements
    audience. Kept as a function rather than a comparison at each call site so
    that "who counts as faculty" has exactly one definition.
    """
    return resolve_role(session_role, is_guest=is_guest) == FACULTY


def announcement_audience(role: str) -> str:
    """
    Map a role onto an audience for `rag/announcements.py`.

    Students and guests both get `"students"` — which still includes every
    `audience="all"` notice, so a guest asking during a typhoon is told classes
    are suspended. Only the faculty-scoped notices are withheld, and those are
    withheld from guests for the same reason they are withheld from students:
    an internal notice reaching an outsider is the worse of the two mistakes.
    """
    return "faculty" if role == FACULTY else "students"


# --------------------------------------------------------------------------- #
# The prompt block
# --------------------------------------------------------------------------- #

# Written as guidance about *emphasis*, not as permission. The distinction is
# load-bearing: a model told "you may not reveal X" will sometimes announce that
# it is withholding X, which tells a student there is something to ask for. A
# model told "answer from this person's point of view" simply does.
_FACULTY_GUIDANCE = (
    "The person asking is FACULTY or STAFF.\n"
    "- Answer from their side of the desk. For a process, give the part THEY "
    "perform (submitting grades, endorsing a form, certifying a requirement), "
    "not the part a student performs — unless they are clearly asking on a "
    "student's behalf, in which case give both and say which is which.\n"
    "- Deadlines that apply to them are the ones worth leading with (grade "
    "submission, clearance, load revision), not the student-facing dates for "
    "the same period.\n"
    "- Assume familiarity with the institution: do not explain what CITAS or "
    "SCTI stands for, and do not suggest they log in to see their records.\n"
    "- They may ask about policy on a student's behalf. Answer the policy. Do "
    "NOT produce any specific student's grades, balance or record — you have "
    "access to no student data other than the account holder's own."
)

_STUDENT_GUIDANCE = (
    "The person asking is an ENROLLED STUDENT.\n"
    "- Give the steps THEY take, in the order they take them, including where "
    "to go and what to bring.\n"
    "- Prefer their own situation over the general rule when the STUDENT "
    "PERSONAL RECORD makes it knowable (their program, year level and standing "
    "decide which of several procedures applies to them).\n"
    "- Do not describe internal faculty or registrar workflow unless they ask; "
    "it is not actionable for them and it buries the part that is."
)

_GUEST_GUIDANCE = (
    "The person asking is a PROSPECTIVE STUDENT or VISITOR with no enrolment "
    "record.\n"
    "- Treat every process question as the NEW STUDENT / TRANSFEREE path. Do "
    "not present the continuing-student procedure unless they say they are a "
    "continuing student.\n"
    "- Spell out admission requirements, where to go, and what an applicant "
    "brings. Expand acronyms the first time you use them.\n"
    "- They have no record, so never imply you can see their grades, balance or "
    "schedule. Mentioning that logging in enables personalised help is welcome "
    "ONCE, at the end."
)

_GUIDANCE = {
    FACULTY: _FACULTY_GUIDANCE,
    STUDENT: _STUDENT_GUIDANCE,
    GUEST: _GUEST_GUIDANCE,
}


def role_block(role: str) -> str:
    """
    The `<asker>` section of the system prompt.

    Always returns a block. Unlike the calendar or announcements — which are
    absent when there is nothing to say — there is always *somebody* asking, and
    leaving the audience unstated is what produced the "everyone is a student"
    behaviour this module exists to fix.
    """
    role = role if role in ROLES else STUDENT
    return (
        "<asker>\n"
        f"{_GUIDANCE[role]}\n"
        "</asker>"
    )
