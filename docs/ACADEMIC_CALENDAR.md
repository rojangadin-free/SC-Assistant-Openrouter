# Academic Calendar — teaching the assistant what the date *means*

## The bug

`src/prompt.py` has always interpolated today's date into the system prompt, and
that was never enough. The model knew the date; it did not know what the date
meant to the person asking.

```
student (June 3)  : "when is enrollment?"
assistant         : "Enrollment for the First Semester is from June 1-15."
```

Correct, cited, and useless. Enrollment closes in twelve days and nothing in the
answer says so. The symmetric failure is worse:

```
student (October 4) : "can i still enroll?"
assistant           : "Enrollment runs from June 1-15."
```

Read in October, that sentence sends a student to campus for a window that shut
four months ago.

A date is a fact. **A deadline is a fact plus the distance to it**, and that
second part is arithmetic an LLM should never be asked to do — "is 2026-06-15
more than twelve days after 2026-06-03" is exactly the kind of question models
answer fluently and wrongly.

## The fix

`rag/calendar.py` computes the timing in Python and hands the model a conclusion
it cannot miscompute:

```
<calendar>
Today is June 3, 2026 (Samar College local time).
The timing below is computed from the official calendar. Use these words for
anything about dates — do NOT recompute how many days are left, and do NOT
describe a past date as upcoming.

- Enrollment: OPEN NOW but closes in 12 day(s), on June 15, 2026
  note: Late fee applies after June 10
- Midterm Exams: NOT YET OPEN — starts July 20, 2026 (in 47 day(s))
</calendar>
```

The model's only remaining job is to phrase it. That is the job it is good at.

## Where the dates come from

Deadlines rotate faster than any other fact in the corpus — a fee schedule
changes yearly, an enrollment window moves every semester — so re-uploading a PDF
to correct one date is far too slow. Periods are entered by an admin at
**Dashboard → Academic Calendar** and take effect on the next question, with no
re-index and no restart.

Admin-entered periods outrank anything found in a document, which is the same
precedence rule as `rag/conflicts.py`, for the same reason: a human decision is
newer evidence than a PDF.

## The admin screen

| Section | What it is for |
| --- | --- |
| **Stats + "As of"** | Open / upcoming / finished, computed for any date you type. This is how "will this still be right in October?" gets answered in October *now*, not in October. |
| **Add or correct a period** | Name, start, optional end, optional note. Re-using an existing name **corrects that row in place** — two rows for "Enrollment" would be a data conflict of our own making. |
| **Read dates from the calendar page** | Paste the calendar section of the handbook; every date range found is proposed. Nothing is saved until you click **Use**. |
| **What will the assistant be told?** | The literal `<calendar>` block, for a question and a date you choose. |

That last panel is the only screen in the dashboard that shows the model's
**input** rather than the admin's. It exists because timing bugs are otherwise
invisible: a wrong end date looks perfectly reasonable in a form, and only
becomes obviously wrong when you read *"closes in 40 days"*.

An empty preview is a real result, not a failure — it means the question was not
about timing, so the assistant answers from the documents alone and spends no
tokens on the calendar.

## Design decisions worth knowing

**Only relevant questions get a block.** `is_time_sensitive()` gates on timing
words (including Tagalog — *kailan*, *hanggang*, *huli*). A question about
tuition or a dean gets no calendar context at all. Injecting the full calendar
into every request would cost tokens on questions that never use it and would
encourage the model to volunteer deadlines nobody asked about.

**"Today" is a parameter, not `now()`.** Every function takes an explicit
`today`. A suite that has to wait until June to exercise the June branch is not a
suite; `test_calendar.py` checks the day before, the first day, the last day and
the day after in a single run.

**Timezone is Asia/Manila, hardcoded.** The server may run anywhere; the deadline
is local to the campus. Computing "days until" in UTC puts the boundary in the
wrong place for eight hours a day — precisely the hours when a student checking
the night before a deadline needs it right.

**The last day says "today is the last day"** rather than "0 days left", because
that is the one phrasing a student cannot misread.

**Dates are parsed by shape, not vocabulary.** No list of expected event names,
because such a list silently ignores the one wording the college actually used.
The tradeoff is that a label is only "the text before the date on that line",
which is why the admin confirms every scanned row.

**A matched date span is consumed.** `December 20, 2026 - January 5, 2027`
matches the cross-month range pattern *and* the two single-date patterns inside
it. Without consuming the span, one event arrived at the review screen as three
rows, two of them wrong — the exact duplicate-data problem the rest of this
project exists to remove.

**`note=None` and `note=""` are different.** Correcting only a date used to blank
the note, because "not supplied" and "cleared" were the same value — so *"Late fee
after June 10"* silently vanished from the prompt when someone pushed the end date
back a week. The API only forwards `note` when the client actually sent the field.

**The browser never computes the countdown.** The admin screen renders the
server's numbers. A locally-computed "12 days left" would disagree with what
students are told the moment the admin's clock or timezone differed — and this
screen exists to make exactly that number trustworthy.

## Files

| File | Role |
| --- | --- |
| `rag/calendar.py` | Parsing, classification, prompt rendering, admin store |
| `sc_assistant/admin_calendar.py` | `list` / `save` / `delete` / `scan` / `preview` |
| `rag/chain.py` | Injects the block into the prompt per question |
| `src/prompt.py` | Instructs the model to trust the block over its own arithmetic |
| `test_calendar.py` | 79 checks — parsing, boundaries, timezone, rendering |
| `test_calendar_api.py` | 64 checks — auth, validation, preview, store round-trip |

## Storage

One JSON blob via `rag/store.py`, so it follows the same rule as the other
features: `calendar_periods.json` locally, a DynamoDB item in production, so that
a period saved by one web worker is visible to all of them. See
`SHARED_STORAGE.md`.

## Running the tests

```bash
python tests/test_calendar.py        # logic
python tests/test_calendar_api.py    # HTTP layer
python run_tests.py            # everything
```

Neither calendar suite needs AWS credentials or the embedding model: storage is
redirected to a temp file and the heavy imports are stubbed.
