# Role-Aware Answers

*Who is asking, and what that changes about the answer.*

---

## The bug, first

`ChatState` has carried an `is_faculty` field since the announcements work.
`rag/announcements.py` filters faculty-only notices by it. **Nothing ever set
it.**

```
sc_assistant/chat.py  ->  input_payload = { input, image_data, uid, user_email, data_consent }
rag/chain.py          ->  audience = "faculty" if state.get("is_faculty") else "students"
                                                       ^^^^^^^^^^^^^^^^^^
                                                       always None
```

So the expression evaluated to `"students"` on every request ever made —
including the registrar's, including the deans'. A faculty-targeted announcement
could be posted, would appear in the admin list, would show as live, and would
reach nobody. The feature worked in the store and in the tests; it did not exist
in production.

That is the concrete half of the problem. The broader half is that a question is
not one question:

| Question | A student needs | A teacher needs |
|---|---|---|
| "How do I enroll?" | The steps *they* perform, at the windows they queue at | What they must do for their advisees, and by when |
| "When is the grade deadline?" | When to expect grades | When *they* must submit them |
| "What is the clearance process?" | Who signs their form | What they sign, and what blocks it |

Answering a teacher with the student checklist is not factually wrong. It is
useless, which in a support tool amounts to the same thing.

---

## What was added

**`rag/roles.py`** — three roles, one normaliser, one prompt block.

```python
resolve_role("user")                       -> "student"    # Cognito's default
resolve_role("registrar")                  -> "faculty"
resolve_role("user", is_guest=True)        -> "guest"
role_block("faculty")                      -> "<asker>...</asker>"
announcement_audience("guest")             -> "students"
```

The `<asker>` block joins `<verified_facts>`, `<document_dates>`,
`<announcements>` and `<calendar>` in the system prompt.

---

## Wiring

```
session["role"]  (Cognito custom:role)
    |
    v
sc_assistant/chat.py     "role": resolve_role(session.get("role"), is_guest=guest)
    |
    v
ChatState.role
    |
    +--> announcement_audience(role) --> announcement_block()   which notices
    |
    +--> role_block(role)            --> {asker} in the prompt  which side
```

`is_faculty` is still honoured in `chain.py` so any older caller keeps working,
but nothing needs to set it anymore.

---

## The three views

**Faculty / staff** — "answer from their side of the desk"
- Give the part *they* perform; give both sides only when they are clearly asking
  on a student's behalf.
- Lead with *their* deadlines (grade submission, clearance, load revision).
- Assume familiarity: don't expand CITAS, don't tell them to log in.

**Enrolled student** — the existing behaviour, now stated rather than assumed
- The steps they take, in order, with where to go and what to bring.
- Their own record beats the general rule when it decides which procedure applies.
- No internal registrar workflow unless asked.

**Guest / prospective** — previously indistinguishable from a student
- Every process question is the new-student / transferee path.
- Expand acronyms; spell out admission requirements.
- Never imply a record exists. Mention logging in **once**, at the end.

---

## This is not access control

A file named `roles.py` invites the assumption that it gates data. It does not.

- Personal records stay gated by `uid` + `data_consent` in `rag/chain.py`. A
  teacher asking "my grades" gets *their own* record, because it is fetched by
  their own uid — no prompt text can widen that.
- The faculty block explicitly still refuses other students' data, and says why
  ("you have access to no student data other than the account holder's own").
- The only thing role actually withholds is the faculty announcement feed, which
  was already scoped that way.

If faculty-only *documents* are ever needed, that belongs in retrieval as a
metadata filter. A prompt instruction is advisory and can be argued out of.

### Why the block never says "you are not permitted"

A model told it lacks permission will often *announce* that it is withholding
something — which tells the asker there is something worth pushing for. The block
is written as point of view, not as permission, so it simply answers from the
right side instead of narrating a refusal. `test_roles.py` asserts the words
"permission" and "authorized" do not appear in it.

---

## Defaults, and why they lean the way they do

| Input | Resolves to | Reason |
|---|---|---|
| `"user"` (Cognito default) | student | Almost every account. Resolving this to faculty would hand the faculty feed to the whole college. |
| `""`, `None`, unknown | student | The student view is the one that is harmless to show the wrong person. |
| `is_guest=True` + `role="user"` | guest | A stale session from before guest login wrote `role`. The flag is backed by reality: there is no uid. |
| `is_guest=True` + `role="registrar"` | guest | A guest session has no verified identity at all. |
| `"admin"` | faculty | An admin asking the chatbot is asking as staff. They post the faculty notices; hiding them from the poster is absurd. |

A guest still receives every `audience="all"` notice. A visitor asking during a
typhoon is told classes are suspended — only faculty-scoped rows are held back,
and those are held back from guests for the same reason they are held back from
students.

---

## Tests

```
python tests/test_roles.py     # 56 checks
python run_tests.py      # 16/16 suites
```

The suite pins the failure modes that matter rather than the happy path:

1. An unlabelled account is a student, never faculty (§1).
2. `is_guest` beats the role string, including `"registrar"` (§3).
3. A guest **is** told about the typhoon and is **not** told about payroll,
   end-to-end through the real announcement store (§4).
4. The block is never empty — an unknown role yields the *student* block, not
   silence, because silence is what caused the original bug (§5).
5. The faculty block does not become a data-access grant (§7).

---

## Files

| File | Change |
|---|---|
| `rag/roles.py` | **new** — roles, aliases, audience mapping, `<asker>` block |
| `rag/chain.py` | `ChatState.role`; audience derived from role; `{asker}` passed to the prompt |
| `src/prompt.py` | `{asker}` slot + rule 9 explaining its authority and its limits |
| `sc_assistant/chat.py` | `"role": resolve_role(...)` in the payload — the line that was missing |
| `test_roles.py` | **new** — 56 checks |
| `run_tests.py` | registered as suite 7 |
