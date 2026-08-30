# Answer Quality & Ask-a-Human

Two features, one problem: **the system had no idea whether its answers were any
good, and a student who hit a wall had nowhere to go.**

This document covers `rag/feedback.py`, `rag/escalation.py`,
`sc_assistant/admin_feedback.py` and the chat endpoints that feed them. It is the
third piece of the data-quality set:

| Feature | Question it answers | Docs |
|---|---|---|
| Data Conflicts | "Two documents disagree — which is right?" | `DATA_CONFLICTS.md` |
| Content Gaps | "What are students asking that we can't answer?" | `CONTENT_GAPS.md` |
| **Answer Quality** | **"Are the answers we DO give actually good?"** | this file |
| **Ask a Human** | **"Who helps the student we just failed?"** | this file |

---

## 1. Why a thumbs-up button is not decoration

There used to be a flag button and a report modal, and they were barely used. That
was not a UI problem — it was a cost problem. Reporting took a decision ("is this
bad enough to complain?"), a reason, an explanation, and a submit. Students only
pay that price when an answer is *offensively* wrong.

The dangerous failure is different. It is the answer that is plausible,
well-formatted, cited, and subtly useless: the right tuition table for the wrong
year, the enrollment steps missing the one step that matters. Nobody files a
report about that. They shrug and go ask a classmate — and the system records a
successful interaction.

A thumb costs one click. That is the entire justification: it is the only signal
cheap enough that students will actually give it, which makes it the only signal
that will exist in volume.

### The 👎 leads *into* the report

So the flag button is gone, and the modal with it. A 👎 now opens a small inline
sheet — "What went wrong?" — with one-tap chips: Outdated, Wrong information,
Incomplete, Not what I asked, Something else. **Tapping a chip is what files the
report**, into the same DynamoDB table and the same admin screen as before,
carrying the question, the answer snippet and the retrieved sources.

Two controls asking "was this wrong?" was one too many. It split the same signal
into a channel students used (one click) and a channel they did not (a form), and
the channel they ignored was the one that reached the admin's queue. Replacing
the form with chips keeps the reasoned complaint but drops its price to a tap.

**A bare thumb is deliberately not a report.** The vote is always recorded — it
still counts toward the topic's satisfaction and still names its suspect
documents — but it does not open a ticket. Filing one per thumb meant the queue
filled with rows whose entire content was "someone disliked this", and the
complaints that named a wrong figure or an outdated date were buried among them. A
queue where most rows have nothing to act on stops being read, which costs more
than the rows it collected.

So the two signals stay separate: 👎 is a *measurement*, a reason is a *work
order*. Dismissing the sheet leaves the measurement and files nothing, which is
an honest record of what the student actually said.

`save_report()` still de-duplicates on `msg_id`: picking a second reason for the
same answer corrects that row instead of adding another.

Because the tap is now the submit, the sheet reports failure honestly instead of
always confirming — if that request does not land, nothing was filed, and telling
the student otherwise would guarantee they never mention it again.


### What makes it more than a vanity counter

Every vote is stored **with the retrieved sources that produced the answer**.
That single decision turns a popularity counter into three tools:

1. **A quality metric per topic.** Satisfaction is computed per normalized topic,
   not globally, so "enrollment answers are fine, fee answers are terrible" is
   visible instead of averaged away.

2. **A suspect list of documents.** If `Samar-College-2024.pdf|p.40` appears under
   six downvotes, that page is wrong, stale, or badly chunked. The admin gets a
   *filename and page*, not a vague complaint.

3. **A regression set.** Every upvoted question is a case whose retrieval is
   known-good. `GET /admin/feedback/api/regression` exports them with their
   expected sources, ready for `eval_retrieval.py`. After changing chunk size or
   the reranker, you can prove the answers that used to work still work — instead
   of spot-checking three questions and hoping.

Point 3 is why the sources are stored rather than just the verdict. **A vote
without its evidence tells you something is wrong but never what.**

---

## 2. Design decisions worth defending

### Aggregates are recomputed, not incremented

`_rebuild_topic()` derives `up`/`down` from the raw votes every time. The obvious
alternative — `entry["down"] += 1` — breaks the moment a student changes their
vote, because that requires *decrementing* the old verdict. Counters that only
ever go up drift away from the votes they claim to summarise, and a metric you
cannot trust is worse than no metric at all.

### One vote per message

Votes are keyed by `msg_id`, so re-voting overwrites. A student flipping 👎 → 👍
corrects the record rather than stuffing the ballot.

### The message id is minted by the server, not the browser

That key only works if it means the same thing tomorrow. It used to not: the id was
generated in JavaScript when the bubble was drawn, so reopening a conversation
produced brand-new random ids for answers that already had votes. The thumb came
back un-pressed, the stored vote pointed at an id that existed nowhere, and the
student's honest conclusion was that their feedback had been thrown away.

Now `/chat/get` mints the id, saves it on the assistant turn, and returns it on the
SSE `done` event so the live bubble adopts it. The **citations are persisted on the
turn for the same reason** — the "Based on" footer used to arrive with `done` and
live only in the DOM, so a reopened answer lost its provenance and looked like an
answer that never had any. `rag/chain.py` reads only `role`/`content` from history,
so the extra keys cost the model nothing.

Conversations saved before this change simply have no `msg_id`; they still render,
they just cannot carry a vote back. Worth preferring over a migration that would
invent ids the old votes were never filed under.


### Guests may vote

Their answers come from the same retrieval pipeline, so their opinion is exactly
as diagnostic as a signed-in student's. Requiring login here would silence the
largest group of users to protect a metric nobody is gaming.

### Topic grouping is borrowed from `rag.gaps`

`normalize_question()` is imported rather than reimplemented. If Answer Quality
and Content Gaps used different definitions of "the same topic", the two admin
screens would disagree about the same traffic — and the admin would have no way to
tell which one was lying.

---

## 3. Ask a Human — the other half of a content gap

`rag/gaps.py` fixed the admin's half of an unanswered question: the topic gets
logged and ranked so the right document eventually gets uploaded. The student in
front of the screen still got nothing, and "eventually" does not help someone
enrolling this week.

```
student asks -> no grounded answer
    ├─ gap logged            (fix the documents)      rag/gaps.py
    └─ "Ask the registrar"   (fix it for THIS student) rag/escalation.py
```

Same question, two outcomes: the corpus improves *and* this student gets an
answer.

### Why it is a separate queue from reports

A report says "this answer was wrong". An escalation says "nobody has answered me
yet". They need different queues because they need different actions — a report is
*reviewed*, an escalation is *replied to*. Merging them buries real questions
under quality complaints.

### A contact detail is required

Signed-in students are reachable by their session email. Guests must supply an
email or mobile number, validated by `valid_contact()`. An escalation nobody can
reply to is not a request, it is litter: it consumes admin attention and returns
nothing.

### The reply text is stored, not just sent

`answer_escalation()` keeps the reply on the row. That reply *is* content — it is
the raw material for the document that should have answered the question in the
first place, and it pairs with the Content Gaps row for the same topic (same
`topic_key`). "Answer the student" and "fix the corpus" become one workflow.

### Routing is data, not UI

`ROUTES` maps `registrar`, `cashier`, `admissions`, `guidance`, `it`, `admin` to
display labels, served to the frontend by `GET /chat/escalate/routes`. Adding an
office is a one-line change, and the UI list cannot drift out of sync with what
the API accepts.

---

## 4. API

### Student-facing (`sc_assistant/chat.py`)

```
POST /chat/feedback          {msg_id, verdict:"up"|"down", question, answer,
                              sources[], comment}
POST /chat/feedback/batch    {msg_ids[]} -> {votes: {msg_id: "up"|"down"}}
                             a "down" replies {can_report: true} — ask the reason
POST /chat/report            {msg_id, reason, other_text, question, sources[]}
                             THIS creates the report. Signed-in students only.

GET  /chat/feedback/<msg_id> restore the button state when a chat is reopened
POST /chat/escalate          {question, route, contact, note, conv_id, answer}
GET  /chat/escalate/routes   the offices a question can be sent to
GET  /chat/escalations/mine  the asker's own queue + unread count
POST /chat/escalations/<id>/read  acknowledge a reply
```

### The reply has to come back inside the app

`/chat/escalate` ends by promising that an office will reply. For a while that
promise was only half-true: the reply was written into `escalations.json`, where
the admin could see it and the student could not. The student was told to watch
an inbox that the college does not actually send mail to yet.

`GET /chat/escalations/mine` closes the loop. It is deliberately narrow:

* **Scoped to the asker.** Rows are matched on the same identity used to file
  them — `uid`/email for a signed-in student, the remembered contact for a guest.
  An escalation queue that leaked across users would expose other people's
  problems *and* their phone numbers, which is a worse failure than having no
  inbox at all.
* **`reply_to` is stripped.** The asker already knows their own contact detail,
  so returning it buys nothing and turns any future scoping bug into a data
  breach instead of a display bug.
* **Pending rows are returned too, and are not counted as unread.** Silence is
  the actual complaint students make about any request sent to an office;
  "received, waiting" is information. But it is not *news*, so it must not light
  up a notification badge — only an answered row does that.
* **Read state lives on the escalation** (`read_at`), not in the browser.
  `localStorage` would forget on a different device and lie after a reinstall,
  and this is precisely the kind of message a student re-checks from their phone.


### Admin-facing (`sc_assistant/admin_feedback.py`)

```
GET  /admin/feedback/api/list?verdict=down       topics, WORST first, + stats
POST /admin/feedback/api/delete                  {key}
GET  /admin/feedback/api/regression?min_upvotes=1  approved Q/A + sources

GET  /admin/feedback/api/escalations?status=pending&route=registrar
POST /admin/feedback/api/escalations/answer      {id, reply}
POST /admin/feedback/api/escalations/status      {id, status}
POST /admin/feedback/api/escalations/delete      {id}
```

**Ordering is deliberate and opposite in the two queues.** Answer Quality sorts by
*most downvotes* — the admin's question is "what is broken?", and a list sorted by
volume tells you only what is busy. Escalations sort *oldest pending first* —
every row is one real person waiting, so the one who has waited longest goes on
top.

---

## 5. Storage

| File | Contents | Env override |
|---|---|---|
| `answer_feedback.json` | votes keyed by `msg_id` + derived topic aggregates | `ANSWER_FEEDBACK_FILE` |
| `escalations.json` | the ask-a-human queue | `ESCALATIONS_FILE` |

Both use the same JSON + `threading.Lock` + atomic-replace pattern as
`rag/conflicts.py` and `rag/gaps.py`, so this codebase has **one** storage story
rather than four. Both are bounded (`MAX_VOTES`, `MAX_ESCALATIONS`) because they
drive prioritisation, not audit. Both are gitignored: they are runtime state, and
one deployment's votes are meaningless in another's checkout.

`record_vote()` and `create_escalation()` swallow their own exceptions. They are
called from the chat path, where a logging failure must never cost a student the
answer they already received.

### Migrating to DynamoDB later

Swap the bodies of `_load()`/`_save()`. Nothing else changes — the same seam that
`DATA_CONFLICTS.md` and `CONTENT_GAPS.md` describe. Worth doing once there are
multiple app instances, since a JSON file is per-container.

---

## 6. Tests

```bash
python tests/test_feedback_api.py    # 104 assertions

python run_tests.py            # all five suites
```



`test_feedback_api.py` redirects storage to a temp directory and stubs boto3,
Pinecone and langchain before importing the app, so it needs no credentials, no
model download, and never touches real data.

The assertions that matter most:

* **sources survive the round-trip** — a vote that loses them is a silent failure
  that only shows up months later when the regression export turns out to be
  empty;
* **a bare thumb files no report, and a reason files exactly one** — the stub
  records `save_report()` calls instead of ignoring them, because both endpoints
  return 200 either way. Getting the first half wrong buries the real complaints
  in noise; getting the second half wrong empties the queue entirely and looks
  like happy students;
* **the filed report carries its evidence** — the document to fix and the question
  that exposed it, since a reason with no evidence just sends the admin hunting;
* **the reason sheet updates rather than duplicates** — `/chat/report` is asserted
  to reuse the same `msg_id`, so one bad answer cannot become two rows;

* **re-voting replaces** — proves the derived-aggregate design actually works;
* **the regression export excludes downvoted topics** — a "known-good" set
  containing a bad answer would enshrine a bug as the expected result, which is
  worse than having no test set;
* **a guest escalation without a contact is refused**;
* **non-admins get 403 on the escalation queue** — it contains student contact
  details, and that is a privacy boundary, not a nicety;
* **`/chat/escalations/mine` returns only the caller's rows** — the student inbox
  is the one place where a scoping mistake becomes a privacy incident, so it is
  asserted from both sides: the student must not see the guest's question, and the
  guest must not be able to mark the student's reply as read.


