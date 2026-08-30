# Announcements

**The problem:** every source of truth in this system is a document, and no
document knows a typhoon is happening.

A student asks *"may klase ba bukas?"* during a storm. Retrieval works perfectly,
finds the student handbook, and answers with the regular class schedule — cited,
confident, and wrong in the way that sends someone out into a storm. Editing the
handbook, re-uploading and re-indexing takes longer than the suspension lasts, so
the document route is not a slow fix here; it is no fix at all.

`ACADEMIC_CALENDAR.md` handled the **scheduled** version of this (deadlines known
months ahead). This handles the **unscheduled** version, which is the more
dangerous half:

| | Calendar | Announcements |
|---|---|---|
| Known when? | in advance | minutes ago |
| Typical item | "Enrollment: June 1–15" | "Classes suspended tomorrow" |
| Reaches the student | only if they ask | **pinned, unprompted** |
| Cost of being wrong | a late fee | a student out in a typhoon |

---

## What one admin action does

Posting an announcement does **two** things from a single record:

1. **Pins a banner** at the top of the chat, so a student who never thinks to ask
   still sees it.
2. **Injects it into the prompt** above the retrieved documents, so a student who
   *does* ask gets the new answer instead of the handbook's answer.

Doing only (1) is a noticeboard nobody reads. Doing only (2) means the
information exists but only reaches people who guessed the right question. Both
matter, and neither is worth a second admin workflow.

---

## Files

| File | Role |
|---|---|
| `rag/announcements.py` | storage, live-window logic, the `<announcements>` prompt block |
| `sc_assistant/admin_announcements.py` | admin CRUD API + the one public endpoint the banner calls |
| `sc_assistant/templates/dashboard.html` | the Announcements screen (post / edit / take down / preview) |
| `sc_assistant/templates/chat.html` + `static/js/chat.js` | the pinned banner students see |
| `rag/chain.py` | inserts the block into every request |
| `test_announcements.py` | 44 checks, offline |
| `test_announcements_api.py` | 81 checks over HTTP, offline |



---

## Design decisions worth knowing

### Highest authority in the prompt, deliberately
The block does not say "you may also consider". It says the announcement
**overrides** the documents, because that is literally the situation: the notice
exists precisely because a document is temporarily wrong. A polite hedge loses to
a handbook paragraph that states class hours as fact.

### Injected into *every* request, with no keyword gate
The calendar block only loads for timing questions. This one always loads. A
suspension is relevant to "is the library open", "should I come tomorrow",
"what time is my class" and phrasings nobody has thought of yet — enumerating
them in advance is exactly the guess that cannot be got wrong here. The cost is a
few dozen tokens.

### Everything expires, and expiry is a date, not a delete
A permanent "classes suspended tomorrow" is a lie by next week. The admin sets an
end date and the notice stops reaching students without anyone remembering to
clean it up. The record itself stays, because "what were students told, and when?"
is the first question asked after an incident.

**The end date is inclusive.** "Suspended until Friday" is live *all* of Friday.
An exclusive end would take the notice down on the morning of the day it is
about.

### Pinned ≠ active
Pinning controls the banner only. An unpinned announcement still reaches the
prompt. "The registrar closes at 3pm today" should correct the assistant's answer
without demanding a banner from everyone who opens the app.

### Priority is display, not truth
All live announcements reach the model. `urgent` decides what a student sees
**first** and how loud it looks — and since students read the top notice and often
stop, ordering is a correctness concern, not decoration.

### Audience is derived server-side
`/api/announcements` reads the audience from the session, never from a query
parameter. Trusting the client would let any student read faculty-only notices by
editing a URL. The public payload also omits `posted_by`: staff names have no
reason to leave the admin screen.

### The banner is polled, and dismissal is content-keyed
The tab stays open for hours, so a notice posted at 6am must reach someone who
loaded the page at 5:50 — hence a 2-minute poll rather than a render-once.

Dismissal is stored in `localStorage` keyed to the notice's **content**, not its
id. If an admin edits *"suspended until Friday"* into *"suspended until Monday"*,
the key changes and the banner returns. Keying on the id alone would hide the
correction from exactly the students who dismissed the original. Server-side
"read" state would be wrong too — a suspension is not a message to one person.

The key is also namespaced per account. `localStorage` is per **browser**, so on
a shared campus PC one student dismissing a suspension used to hide it from the
next person who logged in on that machine — a student who was then never told
classes were cancelled. `/api/announcements` returns an opaque `viewer` tag
(a truncated hash of the session id, *not* the email, which would otherwise sit
in `localStorage` for the next user to read) and every dismissal key carries it.
Before the first response the tag is `anon`, which can only ever mean *show the
notice*: showing a suspension twice costs nothing, hiding it once is the failure
this feature exists to prevent.


---

## Using it

**Post one:** Dashboard → Announcements → headline, urgency, audience, *Until*
date → Post.

Write the headline as the thing itself — *"Classes are suspended on August 22"*,
not *"Important notice"*. It is the line a student reads, and often the only one.

**Check it will actually land:** the *"What will the assistant be told?"* panel
shows the literal text the model receives, for a chosen audience and date. An
announcement can read perfectly in the form and still never reach anyone — wrong
audience, start date in the future, already expired — and this is where that
becomes visible.

**Take it down:** *Take down* hides it but keeps the record. *Delete* is
permanent, and normally the wrong choice.

---

## Tests

```
python tests/test_announcements.py       # 44 checks — the live-window logic
python tests/test_announcements_api.py   # 81 checks — the endpoints, over HTTP

python run_tests.py                # all 10 suites
```

Neither needs AWS, credentials or a model download.

The unit suite concentrates on the failures that are invisible in the UI and
expensive in the chat: both boundary days of the live window, a faculty-only
notice leaking to students, an expired notice still reaching the prompt, and the
ordering that decides which notice gets read.

The API suite covers what only the HTTP layer can get wrong, including one
security boundary rather than a bug: **a student appending `?audience=faculty`
must not be promoted.** `pinned_announcements()` is correct either way — the
question is who gets to choose its argument, and no unit test on that function
can answer it. It also pins the fact that the banner endpoint answers a guest at
all (that route is public on purpose: someone checking "is the campus open?"
during a storm has no account) while still withholding `posted_by`, and that an
edit corrects the existing notice instead of creating a second live one — two
contradicting notices for one event being the exact thing this feature exists to
prevent.


