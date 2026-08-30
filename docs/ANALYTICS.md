# Usage Analytics

**The question this answers:** *"Is the assistant working, and is it getting
better or worse?"*

Every other admin screen answers a question about one row — which dean is
correct, is this deadline still open, did this answer help. None of them answers
the one a panel or a registrar will actually ask. This does.

---

## Why it stores nothing

Four stores already hold everything needed:

| Store | What it already records |
|---|---|
| `rag/feedback.py` | every 👍/👎, with the question and the source pages |
| `rag/gaps.py` | every question the documents could not answer |
| `rag/escalation.py` | every question that needed a human, and whether it got one |
| `rag/conflicts.py` | every contradiction an admin has adjudicated |

`rag/analytics.py` is the **join** over those four. It adds no new table, no new
log, and no new write path — deliberately.

A metric with its own write path is a metric that can *disagree* with the screen
it came from. The moment "Content Gaps: 12 open" sits next to "Analytics: 9
unanswered topics", both numbers are dead: nobody can tell which one lied, so
nobody trusts either. Deriving everything means the dashboard is always
reproducible by re-reading the same four files, and a disagreement is impossible
by construction.

---

## The metrics, and why these ones

**Answered rate** — of the questions we have any signal about, how many the
documents actually covered. This is the headline, because it is the number that
*moves when an admin does the right thing*: upload the missing PDF, and it goes
up. It is the scoreboard for the Content Gaps screen.

**Satisfaction** — of the answers students judged, how many helped. Kept
separate from the answered rate on purpose. "We found something" and "it helped"
are different failures with different fixes — retrieval vs. document quality —
and a single blended number hides both.

**Peak hours, in Manila time.** The stores write UTC, which is correct for
storage and wrong for display: 8 AM on campus is 00:00 UTC, so a UTC histogram
puts the entire morning rush on the *previous day*. Conversion happens once, in
`_parse()`, using the same `MANILA` offset `rag/calendar.py` already uses — not a
second private copy that could drift out of step with the deadline countdowns.

**Week-over-week** — the last 7 days against the 7 before. A lifetime average
cannot distinguish "we fixed it last week" from "it was always fine", and that
distinction is the only one that leads to a decision.

**Top topics** — grouped with `normalize_question()`, the *same* normaliser the
gap log and feedback store use. If analytics grouped differently, the top topic
here would not match the top row there, and the first admin to notice would stop
believing the page.

**Suspect documents** — the join that makes a downvote actionable. A 👎 alone
says an answer was bad; the sources stored alongside it say *which page* produced
it. `Samar-College-update.pdf p.15` climbing this list is exactly how the wrong
dean surfaces without anyone filing a report. Upvotes are counted too, because a
page under fifty good answers and two bad ones is not the problem — the ratio is.

**Escalation clock** — average and median wait, plus the age of the oldest
pending request. An escalation queue with no clock degrades silently: nothing
errors, the rows just sit, and the student promised "someone will reply" learns
the promise was worthless. The median is reported alongside the mean because one
request answered three weeks late would otherwise make a same-day queue look
broken.

---

## The one design decision worth arguing about

**`None` is not `0`.**

```python
def _rate(part, whole):
    return round(part / whole, 3) if whole else None
```

A 0% answered rate and "nobody has asked anything yet" render identically in a
chart but mean opposite things — one is a broken corpus, the other is a system
nobody has used. A fresh deployment showing a confident **0%** invites someone to
go fix a problem that does not exist.

So absence is representable, and it propagates: a quiet day in the series has
`answered_rate: null`, and a week-over-week delta is `null` when either side has
no data, rather than a plausible-looking `0.0`. The UI's job is to render that as
*"not enough data"*.

### One deliberate under-count

Gap events come from each recorded **example**, not from the group's `count`.
A group's counter is a running total with no timestamps, so it cannot be placed
on a time axis at all. Using examples means a topic asked more times than
`MAX_EXAMPLES_PER_GAP` is under-counted here — which is the right way to be
wrong: the trend line stays honest about *when* things happened, and
`gap_stats()` on the Content Gaps screen remains the single authority on totals.

---

## API

All admin-only. All **read-only** — there is no `POST`, and there should never be
one. Every figure is derived from what actually happened, so the only honest way
to change a number is to change the underlying events. An endpoint that could
edit a metric would turn evidence into decoration.

```
GET /admin/analytics/api/overview?days=14
GET /admin/analytics/api/topics?kind=unanswered&limit=20
GET /admin/analytics/api/documents?limit=20
```

`overview` returns everything in **one** payload, not six endpoints, because
these numbers get quoted against each other ("78% of 214 questions"). Six
requests can interleave with a student's vote and render a page whose headline
and chart disagree by one — the exact off-by-one that makes an admin distrust
every other figure on the screen.

`?days=` is clamped to 1–365 rather than rejected: a stray bookmarked
`days=99999` should show a year, not a 400. But it *is* clamped, because
unbounded input means an unbounded response.

---

## Tests

```
python tests/test_analytics.py      # 75 checks
```

The four claims most likely to be quietly wrong, all asserted:

1. **Timezone.** A vote seeded at `23:30 UTC` on Mar 9 must appear at **07:30 on
   Mar 10** — wrong day *and* wrong hour if bucketed in UTC. Nothing about the
   chart would look broken if this regressed, which is why it is test #1.
2. **`None` vs `0`.** `_rate(0, 0) is None` but `_rate(0, 5) == 0.0`; quiet days
   carry `null`; an empty window yields no delta.
3. **Shared grouping.** The latin-honors key computed here equals
   `normalize_question()`'s, and ties break deterministically so the dashboard
   does not reorder itself between refreshes with no data having changed.
4. **Access + immutability.** Students and anonymous users get 403; `POST` to
   `overview` is 405.

Plus: a deliberately corrupt timestamp is skipped rather than blanking the
dashboard, and completely empty stores read as "nothing yet" — zero counts,
`null` rates, no peak hour claimed — while still returning 200.
