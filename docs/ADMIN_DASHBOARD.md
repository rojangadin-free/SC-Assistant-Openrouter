# Admin dashboard: upload progress, analytics, styling, the landing screen


Reported in turn: the upload progress bar was fake, the Analytics screen did not
exist, the admin CSS was an unmaintainable pile of inline styles, the Overview
screen's activity list was inaccurate, Overview and Analytics turned out to be
showing the same figures twice, the sidebar was in build order, and the surviving
landing screen was called two different names while a reload always threw you
back to it. This is what each turned out to be and how it was fixed.



---

## 1. The progress bar was lying

**Symptom.** Uploading a PDF showed a bar that marched to 90% and stopped, then
jumped to 100% whenever the request happened to return.

**Cause.** The percentage came from a `setInterval` timer in `dashboard.js`. It
was never connected to the indexing work. Meanwhile the upload request ran the
whole embed-and-index pipeline synchronously inside the HTTP handler, so a large
document could hold the worker until the browser timed out — and the bar would
sit at 90% looking healthy the entire time.

**Fix.**

- `rag/jobs.py` — a small job-progress store. Deliberately not an in-process
  dict: gunicorn runs two workers, so the request that polls for status is
  frequently *not* the one doing the work, and an in-memory dict would report
  "job not found" at random. It reuses the same `rag/store.py` backend as the
  rest of the app, so it works off a local file in development and DynamoDB in
  deployment with no code change.
- `store_index.py` — accepts an optional `progress` callback and calls it at the
  points that actually take time: each page parsed, each embedding batch sent.
  Default `None`, so the CLI path is unchanged.
- `sc_assistant/admin.py` — the upload handler saves the file, starts a worker
  thread, and returns a job id immediately. Added `GET /admin/upload/status`.
- `dashboard.js` — polls that endpoint and reports the real phase and counter
  ("Reading document — page 7 of 40"), per file.

Progress is now per file rather than one averaged bar, because when one document
in a batch of five fails, an averaged bar hides which one.

### Follow-up: the file list needed a manual reload

Reported after the above shipped: an uploaded file did not appear in "Uploaded
files" until the page was reloaded.

The root cause was the service worker. `NEVER_CACHE` lists `/admin`, but the
admin blueprint is registered at the **root** — its routes are `/files`,
`/upload` and `/dashboard`, none of which start with `/admin`. So `GET /files`
fell through to the shell's stale-while-revalidate branch and was answered from
cache: the list as it stood *before* the upload. The reload "fixed" it only by
giving the background revalidation time to land. `/upload/status/<id>` had the
same hole, which is worse — cached progress is a bar frozen at whatever
percentage was cached.

Fixed by adding `/files`, `/upload`, `/dashboard` and `/delete` to `NEVER_CACHE`,
and bumping `CACHE_VERSION` to v3 so caches already holding a `/files` response
are dropped. `tests/test_pwa.py` now asserts each of those paths, with a note
that admin routes live at the root — this is easy to reintroduce because the
section is called "admin" while the paths are not.

Three smaller contributors fixed at the same time, each of which could produce
the same symptom on its own:

- **The DynamoDB scan was eventually consistent.** `/files` is requested the
  instant indexing finishes, so the row `save_file_metadata()` had just written
  was often absent from the response. Now `scan(ConsistentRead=True)`; double the
  read capacity on a table with one row per document is not a real cost.
- **Two poll paths gave up without refreshing.** The `!res.job` branch and the
  `.fail()` handler both stopped polling and returned. Losing the progress record
  says nothing about whether the file was indexed, and one of them told the admin
  to "check the file list" without refreshing it.
- **The browser's own HTTP cache.** `refreshFileList()` used a plain `$.get`
  against a response with no cache headers. Now `cache: false`, which appends
  `_=<timestamp>`. The list is also sorted newest-first, so a just-indexed file
  appears at the top instead of somewhere inside a scan's arbitrary order.

The service-worker cache was the one that survived a hard refresh, and the only
one of the four that fully explains "but it works after I reload".


## 2. Analytics: the backend already existed

**Symptom.** No Analytics screen.

**Cause.** `rag/analytics.py` and `sc_assistant/admin_analytics.py` were both
complete, registered, and covered by `tests/test_analytics.py` — 75 passing
assertions against three endpoints. There was simply no sidebar entry and no
markup. The feature was finished and unreachable.

Also found while wiring it up: `dashboard.js` built two Chart.js charts from
hardcoded arrays (`[65, 78, 90, ...]`) against canvas ids that no longer existed
in the template. Dead code drawing invented numbers. Deleted.

**Fix.** Sidebar entry, then a section rendering what `/admin/analytics/api/overview`
already returns: four KPIs, a daily questions/refusals chart, an hourly
distribution, a refusal-rate doughnut, and the top-topics and per-document
tables. No new backend code.

Two details worth keeping:

- Charts are destroyed before being recreated. Chart.js keeps a registry keyed by
  canvas, and re-initialising over a live instance leaves the old one attached —
  the visible symptom is tooltips from stale data.
- The chart containers have a fixed height. `maintainAspectRatio: false` makes a
  canvas grow to fill its parent, and a parent sized by its content grows to fit
  the canvas; together they produce a canvas that expands on every resize.

## 3. Inline styles

**Symptom.** ~350 `style="..."` attributes across the admin sections. Changing
the look of a button meant finding every copy.

**Cause.** Each admin section was built separately and styled in place. Most of
the attributes are inside JavaScript string concatenation, not plain markup,
which is why they were never cleaned up.

**Fix.** `sc_assistant/static/css/admin-components.css` — a set of `sc-*`
primitives (cards, buttons, badges, form controls, notes, tables, layout
helpers) extracted from the repeated strings, applied by
`tools/refactor_admin_styles.py`.

The refactor was done by script rather than by hand for a specific reason: 225
edit sites is enough that a manual pass will introduce a typo, and a typo in a
class name produces an unstyled element rather than an error. The script holds an
explicit table of `exact style string -> class list`, so the mapping is
reviewable in one screen and applied identically everywhere.

The rule that makes it a refactor and not a redesign: **every class contains
exactly the declarations it replaced.** Nothing added, nothing dropped. Where a
base class is combined with a modifier, the union is exactly the original
declaration list. No pixel moved.

Result: 225 attributes replaced, template 19.5 KB smaller, 152 inline styles
left — those are the genuinely dynamic ones (`width:' + pct + '%'`, per-item
accent colours) which belong inline.

### Checks that run against it

```
python tools/verify_admin_styles.py    # structure + extracts the inline script
node --check _inline_dashboard.js      # the extracted script parses
python run_tests.py                    # 19/19 suites
```


`verify_admin_styles.py` looks for the failure modes that a browser will not
report:

- **Two `class` attributes on one tag.** The second is silently ignored, so half
  the styling disappears with no error. This is the main thing that can go wrong
  when merging a style attribute into an existing class attribute, and the reason
  the script merges rather than appending a second attribute.
- Empty or malformed attributes left by a bad splice.
- Broken JavaScript, since most sites are inside JS strings.

Verified after the run: 0 duplicate class attributes, both JS files parse, the
template parses as Jinja, all 100 emitted classes are defined in the stylesheet
(and none is defined but unused), 19/19 suites pass.


### Re-running

The script is idempotent — a replaced pattern no longer matches — so running it
again is a no-op. `git diff` is the review surface and `git checkout` is the
undo.

### Adding to the admin UI

Use the primitives instead of writing a new inline style:

```html
<div class="sc-card sc-mb sc-accent-blue">
  <h3 class="sc-h3">Section title</h3>
  <p class="sc-lead">What this screen is for.</p>
  <div class="sc-row">
    <button class="sc-btn sc-btn--primary">Save</button>
    <button class="sc-btn sc-btn--ghost">Cancel</button>
  </div>
</div>
```

Keep inline styles only for values computed at runtime. A hardcoded colour or
padding in new markup is a primitive that should have been added to
`admin-components.css` instead.


## 4. The Overview screen was inaccurate

**Symptom.** "Recent Activities" did not match reality. A document uploaded
minutes ago was often missing, while older events sat at the top.

**Cause.** The feed was built from this:

```python
files_table.scan(Limit=5)          # "the 5 newest documents"
conversations_table.scan(Limit=5)  # "the 5 newest conversations"
# ...then sort those by timestamp and show the top 5
```

`Limit` on a DynamoDB scan does not mean "the newest 5". A scan has **no
ordering** — `Limit` only stops it early — so this returned five *arbitrary* rows
and then sorted those among themselves. The output was always sorted, always
plausible, and only correct when the newest row happened to be in the first five
the scan walked past. Sorting after the fact is what hid the bug: the list looked
deliberate.

Two smaller problems in the same screen:

- **`format_time_ago` compared a Manila timestamp against `utcnow()`.** Every
  event logged in the previous eight hours was described as being in the future.
- **The three stat cards each carried a hardcoded `+0% from last month`.** No
  month-over-month figure was ever computed. It was markup.

**Fix.** `rag/activity.py`, a reporting-only module that reads the six stores
that already timestamp everything they write (feedback, gaps, escalations,
conflicts, calendar, freshness), merges them, sorts once, and truncates:

- `feed(limit, extra=..., window_days=30)` — the timeline, newest first.
- `attention()` — the counts of what is waiting for a human.
- `health()` — satisfaction and answered/unanswered.
- `humanize(iso)` — "just now" through "yesterday", then an absolute date past a
  week, in Manila time.

Uploads and new accounts live in DynamoDB and Cognito, which `rag/activity.py`
deliberately cannot import (it must stay usable with no AWS credentials), so
`admin.py` passes them in through `extra=` and they are merged into the same
sort. `_dynamo_activity_rows()` now reads the whole Files table and sorts it
rather than trusting `Limit`: at one row per document that is cheap and always
correct. If the table grows past a few thousand rows, add a GSI on `uploaded_at`
and query it backwards — the shape it returns would not change.

**The screen was rebuilt around a different question.** It used to answer "how
big is this system" (users, conversations, documents). It now answers "what needs
me": a triage queue of students waiting on a reply, unanswered questions and
answers marked unhelpful, each row clicking through to the screen that resolves
it. `/api/dashboard/stats` was deleted rather than left unused — those three
numbers cannot be acted on, and buying them cost a `describe_user_pool` plus two
full-table COUNT scans on every dashboard load.

Numbers refuse to invent good news. With no votes at all, satisfaction is `null`
and renders as `—`, not `0%`; a new deployment and a broken corpus must not look
identical. Unresolved conflicts are deliberately *not* counted here, because
finding them means re-parsing every source PDF — far too expensive for a screen
that loads on every visit.

Every store is read in its own `try`, so one corrupt file costs its own rows and
not the dashboard.

**Tests.** `tests/test_activity.py`, 127 assertions, offline. The one that matters
seeds twelve events at known times, recorded out of order, and asserts the newest
is first out of more events than the feed shows — the exact thing the old code
could not do. The endpoint section then serves two uploads through a stubbed
table, deliberately stored oldest-first, and asserts the recent one appears and
the merge is time-ordered.


## 5. Two screens were reporting the same numbers

**Symptom.** The dashboard opened on Overview, and Overview and Analytics could
disagree. Satisfaction read one figure on one screen and a different figure on
the other.

**Cause.** Not a calculation bug — a duplication. Both screens printed the same
three facts from two different endpoints:

| Fact | Overview ("System health") | Analytics (KPI row) |
|---|---|---|
| Satisfaction | `#healthSatisfaction` ← `/api/dashboard/attention` | `#kpiSatisfaction` ← `/admin/analytics/api/overview` |
| Ratings given | `#healthVotes` ← same | part of the same KPI |
| Unanswered | `#healthUnanswered` ← same | `#kpiAnsweredRate` ← same analytics call |

Both were correct about different things, which is worse than one being wrong.
`activity.health()` counts **all time**; analytics counts the **selected range**
(7 days by default). So 78% and 71% could both be true, displayed 200px apart,
with nothing on screen saying which period either referred to. There was no
reading of that screen that was not misleading.

The two screens were also 90% the same in structure — both a heading, a
stat row and panels — so which one an admin had open was mostly historical
accident. Overview's only unique content was the triage queue and the activity
feed.

**Fix.** Delete the screen rather than reconcile the numbers. Two panels that
must always agree are one panel.

- **Overview is gone.** Its `#overviewSection`, its sidebar item, and the
  "System health" card with all three duplicate elements.
- **The triage queue and activity feed moved onto Analytics**, at the top, above
  the KPI row — "what needs me" before "how are we doing", because the first is
  actionable and the second is context.
- **Analytics is now the landing screen** (`class="dashboard-section active"`),
  so the dashboard opens on the screen that owns these figures.
- **Satisfaction is stated once**, by the KPI row, which already labels its
  period ("vs. previous 7 days"). `/api/dashboard/attention` still returns
  `health` — it is public API and cheap — but `dashboard.js` no longer renders
  it, with a comment saying why.
- All nine "Back to Overview" buttons return to this screen, and the refresh
  interval polls only while that section is visible.

`showAnalytics()` in the template owns the section switch, because it also has to
build the charts on first open — Chart.js measures a `display:none` canvas as 0×0
and keeps that size forever. `dashboard.js` exposes `window.loadTriage` so the
queue is re-read on each open without a second copy of the fetch.

**Tests.** Section 13 of `tests/test_activity.py` asserts against the rendered
template: exactly one section is `active` and it is the landing screen, the
deleted `#overviewSection` / `#overviewMenuItem` / `#backToOverview` ids are
absent, each of the three duplicate elements is gone, each KPI id appears exactly
once, and both moved panels are present exactly once (a "fix" that dropped them
would otherwise pass). Verified to bite by re-inserting a `#healthSatisfaction`
element — the suite fails.

Those absence checks are deliberately by **id**, not by label. They began as
`"Back to Overview" not in html`, which was correct only while the surviving
screen was still called Analytics; §7 renamed it to Overview, and that string now
matches nine perfectly good back buttons. A label is a UI decision and will
change again — the thing that must never come back is a *second section* quoting
the same figures, and that is what the ids identify.


### 5a. The bug that change introduced: the landing screen never loaded

Promoting Analytics to the landing screen broke it, and every assertion above
still passed.

**Symptom.** A fresh `/dashboard` showed the KPIs as `—`, three blank white
chart boxes, "Loading…" next to the window selector, and a header still reading
**"Dashboard"**.

**Cause.** `#analyticsSection` is rendered with `active`, so the admin arrives on
it **without ever clicking the menu item** — and every loader hung off that
click. Nothing fetched. The header text was the visible fingerprint: it is set by
`showAnalytics()`, so "Dashboard" meant that function had never run. Navigating
away and back "fixed" it, which is why it could be missed.

**Fix.** One line at the end of the template's ready handler:

```js
if ($('#analyticsSection').hasClass('active')) showAnalytics();
```

Through `showAnalytics()` rather than `loadAnalytics()`, so there is one
definition of "you are on Analytics now" (header, active classes, figures, triage
queue). It is deliberately the **last** statement: `showAnalytics` reads
`analyticsLoaded`, a `let`, so calling it earlier dies in the temporal dead zone.

**A second failure found while fixing it.** Chart.js comes from a CDN, and
`new Chart` was called directly in the middle of `renderAnalytics()` — so if that
script did not arrive, the exception fired *before* the tables below it were
rendered, and one blocked external file blanked the whole screen including
figures already in hand. All three charts now go through `drawChart()`, which
checks the library loaded, catches a construction failure, and degrades to a
message inside the chart's own box. The numbers are the point; the charts are a
reading of them.

**And a double fetch.** `dashboard.js` also called `loadDashboardData()` at
startup. With the template now loading the landing screen (which calls
`loadTriage`), that would fire the triage and activity requests twice on every
page load. It is now a guarded fallback — deferred a tick, skipped if the
template got there first — so the panels still fill if the inline script ever
fails to run, without duplicating the normal path.

**Tests.** Same suite: that something *calls* the loader on load (not merely that
the markup exists), that the header-retitling line survives, that `new Chart(`
appears exactly once and `= drawChart(` three times, and that the startup triage
call is guarded. All four were mutation-tested — each fix reverted in turn, each
one caught.


## 6. The sidebar was in build order

Ten sections had accumulated one at a time, and the menu listed them in the order
they were written:

```
Analytics · File Uploads · Academic Calendar · Announcements · Content Gaps
Answer Quality · Data Conflicts · Ask a Human · Reports · User Management
Open Chatbot
```

That order is meaningful to whoever added the sections and to nobody else.
Finding a screen meant reading all ten labels, because neighbours had nothing to
do with each other — "Announcements" sat between the calendar and content gaps.
Worse, **"Ask a Human" was eighth**: it is the only screen where a real student is
waiting for a real reply, and it was below three analysis screens and two content
screens.

It is now grouped by the job being done, with the groups in order of urgency:

| Group | Items | Why here |
| --- | --- | --- |
| *(none)* | Analytics | The landing screen. Nothing may sit above it. |
| **Needs attention** | Ask a Human · Reports · Content Gaps · Answer Quality · Data Conflicts | A queue. Someone is waiting on each of these. |
| **Knowledge base** | File Uploads · Academic Calendar · Announcements | What the assistant knows and what it is currently saying. |
| **Administration** | User Management | Real work, done rarely, so it is out of the way. |
| *(after a divider)* | Open Chatbot | Not a section — `target="_blank"`, it leaves the dashboard. |

**The order inside "Needs attention" is not arbitrary.** It matches
`rag.activity.attention()`, which already ranks the same concerns and carries the
comment *"First because a person is actually waiting."* The sidebar and the triage
panel on Analytics now agree about what matters most; before, they disagreed,
which is how an admin learns to trust neither. Data Conflicts is last in the
group for the same reason `attention()` omits it: its count needs a re-parse of
every source PDF, so it is a real problem but not the one to chase first.

Nothing in the menu is positional in code — every item is bound by id
(`$('#uploadsMenuItem').on('click', …)`), which is exactly why this needed a
test. A reshuffle cannot fail loudly; it can only quietly put the rarely-used
screens back on top.

### The bug this uncovered: badges vanished when collapsed

The seven count badges were nested **inside** the label span:

```html
<span>Ask a Human <span id="sidebarEscalationBadge">3</span></span>
```

Collapsing the sidebar applies `.sidebar.collapsed .menu-item span { display:none }`
to hide the labels. A child of a `display:none` element cannot be brought back by
any rule at any specificity — so **every badge disappeared in precisely the state
that most needs them**, where the sidebar is 72px of unlabelled icons and the
badge is the only signal about where the work is.

Badges are now siblings of the label, and collapse to a 9px dot positioned
against the icon. The digits are dropped (`font-size:0`) rather than shrunk: an
unreadable number invites a guess, while a dot honestly says only "there is
something here". The dot rule must restate `display:block`, because a badge is
now a span directly inside a menu item and therefore still matches the hiding
selector above — specificity is applied per declaration, not per rule, so
omitting it silently reintroduces the original bug.

### Two smaller things found in the same file

**The pending-reports count was fetched twice.** `sidebar.html` ran its own IIFE
against `/admin/reports/api/list?status=pending`, and `dashboard.html` requests
the same URL to fill the same element on ready. Every dashboard load asked twice
and the second answer overwrote the first with an identical number. The
`dashboard.html` copy is the one kept — it sits with the other badge updates and
is re-run when a report is resolved, which the sidebar's copy never was.

**Seven copies of one badge style.** Each badge repeated the same five inline
declarations, differing only in colour. They now use the existing `.sc-count`
primitive from `admin-components.css` with colour modifiers. Sharing is safe
because both users are dashboard-only: that stylesheet is loaded by
`dashboard.html`, and the markup needing it is inside
`{% if active_page == 'dashboard' %}`. `--amber` keeps dark text, since white on
`#f59e0b` is about 2.1:1 and unreadable at `.72rem`.

### Tests

`tests/test_activity.py` section 14, asserting **order and grouping** rather than
mere presence: that Analytics leads, that the triage group matches
`attention()`'s ranking, that "Ask a Human" is second overall, that User
Management is last, that the divider follows every section, and that group
headings are not `.menu-item`s — `$('.menu-item').removeClass('active')` runs on
every navigation, so a heading carrying that class could render as "selected".

Then the collapse: every badge asserted to be a *sibling* of its label (the label
span must close before the badge opens), the `display:block` and
`position:absolute` restatements asserted in `main.css`, and the duplicate
report fetch asserted to appear exactly once. Mutation-tested — nesting one badge
back inside its label, and deleting the `display:block`, are each caught.


## 7. One screen with two names, and a reload that undid your work

Two complaints about the same screen, reported together.

### 7a. It was called "Analytics" and "Overview" at the same time

**Symptom.** The sidebar said **Analytics**, the page heading said **Analytics**,
the header bar said **Analytics** — and the nine back buttons on every other
screen said **"Back to Overview"**, returning you to it.

**Cause.** §5 deleted the Overview screen and moved its triage queue and activity
feed onto Analytics. The back buttons were left pointing at the survivor and
never relabelled. So the destination of "Back to Overview" was a screen called
Analytics, and the docs for §5 cheerfully described both names as if they were
different places.

**Fix.** The screen is called **Overview** everywhere a human can read it. That
direction, and not the other, because the name has to describe the content: after
§5 this screen is the triage queue, the activity feed, four KPIs *and* the charts.
"Analytics" describes only the bottom half of it, and nine back buttons already
called it Overview — renaming those instead would have meant changing nine labels
to match one that was wrong.

**The ids were deliberately left alone.** `#analyticsSection`,
`#analyticsMenuItem`, `showAnalytics()`, `backToAnalyticsFromGaps` — all
unchanged. Every one of the ten sections is wired by id across
`dashboard.html`, `dashboard.js`, `sidebar.html` and two test suites; renaming
them is a large, silent-failure-prone edit (a mistyped selector produces a dead
button, not an error) in exchange for nothing a user can see. The gap between the
internal name and the visible label is now stated in a comment at each of the
three places someone would trip over it.

### 7b. A reload threw you back to the landing screen

**Symptom.** Upload a document on File Uploads, reload to confirm it indexed, and
you are on Overview. Every time.

**Cause.** All ten sections live in one HTML document and are switched by adding
`.active`. Nothing was persisted, so a reload re-rendered the default —
`#analyticsSection` is the one with `active` in the markup. Acceptable on a page
you visit; wrong on a page you *work* in, and it interacts badly with §1, where
the natural way to check on a long indexing job is to reload.

**Fix.** `dashboard.js` records the last opened section in `localStorage` and
replays it on load.

What is stored is the **menu item id**, not a section name. Every screen is opened
by clicking a menu item, and those handlers already do the whole job — switch
section, retitle the header, load the data, build charts on first reveal.
Restoring by replaying the click reuses all of it. Storing `"uploads"` instead
would need a second name-to-behaviour mapping, and a second mapping is exactly
what let §5a happen: a section displayed without its loaders ever running.

The recorder is one delegated handler on `.menu-item[id$="MenuItem"]`. Binding
each of the ten individually would mean editing that list for every new screen,
and forgetting is silent. It also cannot match "Open Chatbot", which has no
`MenuItem` id — that link leaves the dashboard and is not a place to restore to.
The nine back buttons do not click a menu item at all, so `showAnalytics()`
records `analyticsMenuItem` itself; without that, "Back to Overview" then reload
would return you to the section you had just left.

Three failure modes, each handled because each is silent:

- **`localStorage` throws.** Private browsing and a full quota both make
  `setItem` raise. Both calls are wrapped — a dashboard must not fail to navigate
  because it could not take a note.
- **A stored id no longer exists.** `localStorage` outlives deployments, so a
  renamed or deleted section would leave `.trigger('click')` matching nothing:
  every section hidden, no section shown, an admin on a blank page with no
  explanation. The id is shape-checked and looked up in the DOM before use, and
  the caller falls back to Overview.
- **The id exists but its handler was never bound.** The trigger silently does
  nothing, so the function reports success by asking whether the **menu item**
  ended up `.active` — only a bound handler can have done that.

That last check is the subtle one, and the first version got it wrong. It asked
whether any `.dashboard-section` was active, which is always true:
`#analyticsSection` ships with `active` in the markup. Restore would report
success having done nothing, the `showAnalytics()` fallback would be skipped, and
the landing screen would sit there with its loaders never run — KPIs on `—`,
canvases blank, header reading "Dashboard". Precisely the §5a bug, reintroduced by
the code meant to avoid it.

**Tests.** `tests/test_activity.py` gains a section for the rename and one for the
persistence. The rename is asserted as *agreement* — the menu item and the `<h1>`
both read Overview, and no visible label or `header-left` assignment still says
Analytics — rather than as a list of strings, so the two names cannot drift apart
again. §5's absence checks moved from label matching to id matching in the same
pass, because `"Back to Overview" not in html` was only ever correct while the
screen had the other name (see §5).

The persistence assertions are deliberately aimed at the failure modes above and
not at the happy path: that a stale id is validated against the DOM, that the
success check is the menu item and *not* `.dashboard-section.active`, that the
recorder is the delegated selector, that the back buttons record, and that both
storage calls are guarded. All six were mutation-tested — each protection removed
in turn, each one caught.




