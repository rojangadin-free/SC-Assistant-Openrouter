"""
test_roles.py — role resolution, the announcement audience, and the <asker> block.

    python tests/test_roles.py

No AWS, no network, no vector store: `rag/roles.py` is pure functions over a
string, so the interesting failures are all reachable in-process. The store is
still redirected to a temp file for the one section that exercises the
announcements audience end to end, because that section writes real rows.

What is actually being defended here
------------------------------------
1. An unlabelled account is a STUDENT. Cognito's default `custom:role` is
   "user", and if that resolved to faculty the faculty announcement feed would
   go to the whole college. Section 1 pins that.
2. A guest is not a student. They have no record, so the useful default is the
   applicant view.
3. The audience mapping does not withhold campus-wide notices from anybody. A
   typhoon suspension must reach a guest; only faculty-scoped rows are held back.
4. The block always exists. Every other prompt section is absent when it has
   nothing to say; this one cannot be, because "nobody said who is asking" is
   exactly the state that made every user a student.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import os
import shutil
import tempfile

# Redirect storage BEFORE importing anything that builds a store.
_TMP = tempfile.mkdtemp(prefix="sc_roles_test_")
os.environ["STORE_BACKEND"] = "file"
os.environ["ANNOUNCEMENTS_FILE"] = os.path.join(_TMP, "announcements.json")

from rag.roles import (                                    # noqa: E402
    resolve_role, is_faculty, announcement_audience, role_block,
    ROLES, LABELS, STUDENT, FACULTY, GUEST,
)
from rag.announcements import save_announcement, announcement_block  # noqa: E402

_passed = 0
_failed = 0


def check(label, condition):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}")


def section(title):
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
section("1. An unlabelled account is a student, never faculty")
# --------------------------------------------------------------------------- #

# This is the one that matters most. Cognito returns "user" for every account
# created without a custom:role attribute — i.e. nearly all of them.
check("Cognito's default 'user' -> student", resolve_role("user") == STUDENT)
check("an empty role -> student", resolve_role("") == STUDENT)
check("None -> student", resolve_role(None) == STUDENT)
check("an unknown role -> student", resolve_role("wizard") == STUDENT)
check("...and none of those are faculty", not any(
    is_faculty(r) for r in ("user", "", None, "wizard", "alumni")
))

# --------------------------------------------------------------------------- #
section("2. The roles that ARE faculty")
# --------------------------------------------------------------------------- #

for label in ("faculty", "teacher", "instructor", "professor", "staff",
              "registrar", "dean", "admin"):
    check(f"'{label}' -> faculty", resolve_role(label) == FACULTY)

check("case and whitespace are tolerated", resolve_role("  Registrar ") == FACULTY)
check("mixed case 'FACULTY'", resolve_role("FACULTY") == FACULTY)

# An admin asking the chatbot is asking as staff, not as a student. They are the
# ones who post the faculty notices; hiding them from the poster is absurd.
check("an admin is treated as staff", is_faculty("admin"))

# --------------------------------------------------------------------------- #
section("3. A guest is its own audience, and the flag wins")
# --------------------------------------------------------------------------- #

check("role='guest' -> guest", resolve_role("guest") == GUEST)
check("is_guest=True -> guest", resolve_role("user", is_guest=True) == GUEST)

# A session created before the guest login started writing role="guest" carries
# role="user" with is_guest=True. The flag is the one backed by reality: there is
# no uid behind it.
check("a stale session's flag beats its role string",
      resolve_role("user", is_guest=True) == GUEST)

# And the inverse must NOT hold: a guest flag on a staff session still means
# guest, because a guest session has no verified identity at all.
check("is_guest=True beats even 'registrar'",
      resolve_role("registrar", is_guest=True) == GUEST)
check("a guest is not faculty", not is_faculty("guest"))
check("every resolved value is a known role",
      all(resolve_role(r) in ROLES
          for r in ("user", "guest", "dean", "", None, "nonsense")))

# --------------------------------------------------------------------------- #
section("4. The audience mapping withholds only faculty-scoped notices")
# --------------------------------------------------------------------------- #

check("faculty -> the faculty feed", announcement_audience(FACULTY) == "faculty")
check("a student -> the student feed", announcement_audience(STUDENT) == "students")
check("a guest -> the student feed too", announcement_audience(GUEST) == "students")

# The guest mapping is the point: "students" still includes every audience="all"
# row, so a campus-wide suspension is not withheld from a visitor.
save_announcement(
    "Classes suspended due to typhoon",
    "All classes are suspended campus-wide.",
    starts_on="2026-06-10", expires_on="2026-06-10",
    audience="all", priority="urgent", posted_by="admin@sc.edu",
)
save_announcement(
    "Faculty payroll cut-off moved",
    "Submit DTRs by Friday.",
    starts_on="2026-06-10", expires_on="2026-06-10",
    audience="faculty", priority="info", posted_by="admin@sc.edu",
)



import datetime  # noqa: E402
_that_day = datetime.date(2026, 6, 10)

guest_block = announcement_block(
    audience=announcement_audience(GUEST), today=_that_day)
faculty_view = announcement_block(
    audience=announcement_audience(FACULTY), today=_that_day)

check("a guest IS told classes are suspended", "typhoon" in guest_block.lower())
check("a guest is NOT told about payroll", "payroll" not in guest_block.lower())
check("faculty see the payroll notice", "payroll" in faculty_view.lower())
check("faculty also see the campus-wide one", "typhoon" in faculty_view.lower())

# --------------------------------------------------------------------------- #
section("5. The block always exists and names the audience")
# --------------------------------------------------------------------------- #

for role in ROLES:
    block = role_block(role)
    check(f"{role}: a block is produced", bool(block.strip()))
    check(f"{role}: it is tagged <asker>",
          block.startswith("<asker>") and block.endswith("</asker>"))

# Unlike the calendar and announcements, an unknown/absent role must NOT yield an
# empty block — silence is what made every user a student by default.
check("an unknown role still yields a block", bool(role_block("wizard").strip()))
check("...and it is the student block",
      role_block("wizard") == role_block(STUDENT))
check("an empty role yields the student block",
      role_block("") == role_block(STUDENT))

# --------------------------------------------------------------------------- #
section("6. The three blocks say materially different things")
# --------------------------------------------------------------------------- #

fac = role_block(FACULTY)
stu = role_block(STUDENT)
gue = role_block(GUEST)

check("all three differ", len({fac, stu, gue}) == 3)

check("faculty are told they are faculty", "FACULTY" in fac)
check("faculty get their own side of the process", "side of the desk" in fac)
check("faculty are steered to their own deadlines",
      "grade submission" in fac.lower())
check("faculty are not asked to log in",
      "log in" in fac.lower() and "do not suggest" in fac.lower())

check("students are told to get the steps THEY take", "steps THEY take" in stu)
check("students are pointed at their own record",
      "STUDENT PERSONAL RECORD" in stu)
check("students are spared internal workflow",
      "registrar workflow" in stu.lower())

check("guests are treated as applicants", "PROSPECTIVE STUDENT" in gue)
check("guests get the new-student path", "TRANSFEREE" in gue)
check("guests are not implied to have a record",
      "never imply you can see their grades" in gue)
check("guests are invited to log in exactly once", "ONCE" in gue)

# --------------------------------------------------------------------------- #
section("7. The faculty block does not become a data-access grant")
# --------------------------------------------------------------------------- #

# roles.py shapes emphasis, not permissions. The faculty block must therefore
# still forbid producing another person's record — otherwise "answer from their
# side of the desk" reads as licence to answer "what is Juan's balance?".
check("faculty are refused other students' records",
      "Do NOT produce any specific student's grades" in fac)
check("...and told the reason (they have no such data)",
      "no student data other than the account holder's own" in fac)

# The word "authorized"/"permission" should NOT appear: telling a model it lacks
# permission invites it to announce that something is being withheld, which tells
# the asker there is something to push for.
check("the block does not talk about permissions",
      "permission" not in fac.lower() and "authorized" not in fac.lower())

# --------------------------------------------------------------------------- #
section("8. Labels exist for every role (admin screens)")
# --------------------------------------------------------------------------- #

check("every role has a label", all(r in LABELS for r in ROLES))
check("no label is empty", all(LABELS[r].strip() for r in ROLES))
check("labels are human, not slugs", LABELS[FACULTY] == "Faculty or staff member")

# --------------------------------------------------------------------------- #
print(f"\n{'='*54}\n  {_passed} passed, {_failed} failed\n{'='*54}")
shutil.rmtree(_TMP, ignore_errors=True)
raise SystemExit(1 if _failed else 0)
