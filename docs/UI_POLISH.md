# UI polish pass

What changed, why, and — for the parts that look wrong but are not — the
measurement that settled it.

The work lives in one new stylesheet, `sc_assistant/static/css/polish.css`,
loaded last on every page. Everything else in this document is a small edit to a
template.

---

## Why a new stylesheet instead of edits in place

The app had six stylesheets that each grew their own literal values. Measured
across them: **62 unique colours, 70 font sizes, 38 spacing values**, with no
scale behind any of them, and **68 text/background pairs below the 4.5:1** WCAG
2.2 §1.4.3 minimum — mostly the same grey (`#9ca3af`) and the same school gold
(`#fbbc05`) repeated file to file.

Fixing 68 pairs at 68 call sites means the next literal hex undoes the work. So
`polish.css` declares the tokens the app was missing, re-points the repeated
offenders at them, and adds the motion and state vocabulary that did not exist.
It sits after every other sheet because equal-specificity rules only win on
source order.

Load order is load-bearing in two places:

- `base.html` links it **after** `{% block extra_head %}`, which is where each
  page pulls in `chat.css` / `dashboard.css`.
- `auth.html` does not extend `base.html`, so it links `polish.css` itself.
  Without that line `/login` was the only page still shipping the unfixed
  values — and it is the first screen every student sees.

---

## Typography

Inter was being pulled in by an `@import` at the top of three stylesheets. An
`@import` is only discovered after the sheet containing it has been downloaded
and parsed, so the font could not start loading until a round-trip had already
been spent: the page painted in a fallback and reflowed when the real face
arrived. It is now a `<link>` in the head with `preconnect` to both Google hosts
(`crossorigin` on the gstatic hint — fonts are fetched in CORS mode, and a hint
without it opens a connection the font request cannot reuse).

`wght@300..800` is the variable font: one file for the whole range instead of
four static cuts. That also fixed real faux-bold — `font-weight: 800` is used by
the dashboard activity feed and was never among the loaded weights, so the
browser was smearing 700 to fake it. The range starts at 300 because the answer
text is set in Light; browsers synthesise bolder weights but never lighter ones,
so `300` against a font starting at 400 would have silently clamped back.

On top of that:

- A **fluid type scale** (`--step--1` … `--step-4`) on a 1.2 ratio, using
  `clamp()` so a phone gets the small end and a desktop the large end with no
  breakpoint to maintain.
- Tighter tracking as size grows. Large type at 0 tracking always looks loose —
  the letterforms were designed for body copy.
- `font-feature-settings: "cv11" 1, "ss01" 1, "calt" 1` — this is what makes
  Inter look like Inter rather than a fallback.
- **Tabular figures** on every counter and KPI, so a count going 9 → 10 stops
  shifting the label beside it.
- Answer prose capped at 68ch. Past ~75 the eye loses the line it is returning
  to.

---

## Motion

Named keyframes rather than per-component one-offs, so the same gesture means
the same thing everywhere. Durations are 90–380ms: past ~400ms a UI stops
feeling like a conversation, and these are decorations on top of work the user
already asked for.

- Page arrival: content region only. Animating the chrome on every navigation
  reads as the app reloading rather than the page changing.
- Chat: the user's bubble comes from the right (where they typed it), the answer
  from the left. Direction carries authorship before colour does.
- Lists stagger over six steps then stop — past that the last row waits long
  enough to feel like a bug.
- Overlays scale up from slightly small, which reads as "this came from the thing
  you clicked".
- Loading is a shimmer, not a spinner: a shimmer says *content is coming and will
  look like this*.

`prefers-reduced-motion` is handled by turning every animated element into its
**finished** state — `opacity: 1`, no transform. A bare `animation: none` would
freeze an element whose first frame is `opacity: 0`, i.e. leave it invisible.
Verified in the browser: 0 elements left mid-flight.

---

## Contrast

Every repair is routed through a token, never a hardcoded light-mode colour, and
each token is declared in **both** themes. That second part matters more than it
looks: an undefined custom property makes the whole declaration invalid at
computed-value time, so `color: var(--undefined)` silently falls back to
`inherit`.

Notable fixes:

| Was | Now | Where |
|---|---|---|
| `#9ca3af` at 2.41:1 (24 sites) | `--ui-muted` #6b7280, 4.83:1 | all secondary text |
| dark active nav at 2.81:1 | `--ui-green-ink` #46c62b, 6.8:1 | sidebar "where am I" |
| dark `.forgot-password` at 2.91:1 | same token | the one link a locked-out student needs |
| `#22c55e` fill, white at 2.28:1 | `--ui-fill-success` #15803d, 5.08:1 | green buttons/counts |

### The part worth reading

The audit reported **seven critical gold-on-light failures at 1.62:1**. Measured
from rendered pixels, six were false alarms: the scanner assumes every `color`
sits on the page background, but `.new-chat-btn i` is inside a button filled
with solid `#0d4503`, and a user's message link sits on the green bubble. Those
measure ~7.5:1. "Fixing" them by darkening the gold would have taken them to
**2.11:1 — worse than the finding**.

The seventh was real. `.samar-college-text:hover` genuinely sends the wordmark to
raw gold on the light sidebar, 1.63:1 confirmed from pixels. The obvious fix
(darkened gold, 5.06:1) reads as a *weaker* colour than the 10.67:1 green it
replaces, so the link would visibly recede at the moment it is pointed at.
Deepening the green instead keeps the brand colour and keeps the hover direction
correct: **10.67:1 at rest → 12.78:1 on hover**.

Two more that only one method could catch:

- **Toast icons.** Reading `getComputedStyle` on the chip returns the toast's own
  background, and mid-fade-in its opacity is 0 — so a hand-composited ratio
  reported 1.3:1 for a chip that actually renders at 9.23:1. Screenshotting and
  histogramming the pixels is the only measurement that cannot be fooled by
  that.
- **`.status-unconfirmed`.** The user table is empty until an admin loads it, so
  the rendered sweep reported the app clean while this badge was still short.
  The static scanner caught it at 4.43:1. Fixed on the selector, not the token,
  because `--ui-warn-ink` is fine on the plain page and used in six other places.

Two audit suggestions were **reverted** after measuring. The "invisible surface
boundary" it wanted a border and shadow on is `height: 1px` — it is the divider
between menu groups, and a border plus shadow on a hairline renders as a fuzzy
3px bar. I also tried repainting it darker and backed that out too: in the
collapsed rail the box measures 20px tall, so a 14% dark fill would have drawn a
grey slab down the sidebar.

---

## Touch targets

Ten controls measured under 44px, one at 9px. `min-width`/`min-height` grows the
hit area without changing the glyph. Deliberate exceptions:

- The badge inside a collapsed menu item is a dot, not a target — inflating it
  to 44px would swallow the row.
- Inline text links (`.forgot-password`, the wordmark) are exempt from 44px under
  WCAG 2.2 §2.5.8 — padding them would break the line they live in. They still
  owe the 24px floor and both measured **23px**, one pixel short; `inline-flex`
  fixes it without moving the baseline.
- `@media (pointer: fine)` drops icon buttons back to 32px. A mouse is precise,
  and the inflated boxes push dense toolbars apart on desktop.

Text inputs get 44px **and** `font-size: max(16px, 1rem)`: anything under 16px
makes iOS Safari zoom the page on focus, leaving the layout scrolled sideways
with no way back except pinching. Relatedly, `maximum-scale=1.0,
user-scalable=no` was removed from the viewport meta — it disabled pinch-zoom,
which is the one tool a student with weak eyesight has, and it was only there to
work around that same 16px rule.

---

## Skip links

The audit said "skip-to-content: NO — add one". Its detector looks for a literal
`href="#main-content"`, and `base.html` emits a Jinja block there. Querying the
served DOM of all four routes instead found the opposite of what either source
claimed: three routes had a working link, and **`/login` had none** — because
`auth.html` is standalone. Added.

Both chat and login autofocus their primary field, so the first Tab does not land
on the skip link; it is reachable from the top of the document or by Shift+Tab,
which is documented inline in both templates so nobody "fixes" the autofocus. On
`/login`: Shift+Tab twice from `#email` → link slides to `top: 8px` with the ring
on → Enter moves focus to `#loginMain`. The targets carry `tabindex="-1"`;
without it the browser scrolls the region into view but leaves focus on `<body>`,
so the next Tab starts over.

---

## The mobile drawer that "couldn't be removed"

Reported after the pass above: on a phone, in the Reports section, the sidebar
menu could not be dismissed. The drawer was not stuck — the page was scrolled
sideways, and the only close control had scrolled off with it.

Measured at 390px on `/dashboard` → Reports:

| | before | after |
|---|---|---|
| `<html>` horizontal overflow | 294px | 0 |
| `.content-area` width in a 390px viewport | 684px | 390px |
| `#hamburgerMenu` position | `left: -270px` (off screen) | `left: 24px` |

`admin-components.css` gives wide tables `min-width: 38rem` inside
`.sc-scroll-x { overflow-x: auto }`, which is correct — the table is meant to
scroll in its own box. It could not, because every ancestor up to the viewport
(`.main-section` → `.page-content` → `.content-area`) is a flex child, and a flex
item defaults to `min-width: auto`: *never shrink below my content's intrinsic
minimum*. The table's 608px floor propagated all the way up and the document grew
to 684px.

That is what made it look like a stuck menu. The header is in normal flow; the
drawer and its scrim are `position: fixed`. Scrolled 294px right, the header —
holding the only visible close button — sits off the left edge while the drawer
stays exactly where it is. What remains on screen is a drawer, no close control,
and a 110px strip of dimmed backdrop that gives no hint it can be tapped.

`min-width: 0` on that chain lets the overflow land in `.sc-scroll-x` where it
belongs. The Reports table now genuinely scrolls inside its box (608px of content
in a 314px scroller) instead of pushing the layout.

**The same defect existed above the mobile breakpoint, and scoping the rule to
768px hid it.** The first verification pass came back clean at 390 and 1440 —
both real widths, neither of which exposes it. At **820px**, a tablet width, 37px
of sideways scroll was still there: with the sidebar back to its permanent 260px,
`.main-section` had 560px of free space and rendered 596px, because probing it
with `width: min-content` returns exactly 596. Same mechanism, different
breakpoint, so the rule now lives outside the media query. Checking only the
widths in the original brief would have shipped this.

Two more things fell out of chasing that number:

- **The last 11px was an invisible dialog.** `.confirm-dialog` (`position: fixed;
  inset: 0`) is hidden with `visibility: hidden; opacity: 0` but left at
  `display: flex`, and a visibility-hidden box still generates layout — all three
  copies in the DOM kept contributing to scroll width, measured with a right edge
  of 846px in an 820px viewport. This is why sweeping the active section for
  overhanging elements came back empty while the overflow persisted: the offender
  had nothing to do with the section on screen. `display: none` while inactive
  removes it; the `.active` class still re-asserts `display: flex`, verified by
  opening the logout dialog after the change.
- **Choosing a section did not close the drawer.** The dashboard swaps sections
  in place rather than navigating, so nothing ever removed `.active` — the admin
  tapped "Reports", the section changed *behind* the drawer, and on a 390px
  screen the drawer covers 280px of it. Handled in `main.js`, mobile only, with a
  180ms delay so the section switch paints before the drawer slides away and the
  two movements read as one action. Delegated from `document` because the
  dashboard binds its own handlers to the same elements.

Verified against the real files, no injected CSS: all **ten** dashboard sections
at 390px open the drawer, close it on selection, clear the scrim, and report 0
overflow with the hamburger on screen. 0 overflow at **390 / 430 / 768 / 820 /
1024 / 1440**. Desktop is untouched — at 1440 the sidebar stays permanent, the
mobile close logic is correctly inert, and the Reports table keeps its full
1114px with no inner scrollbar, because `min-width: 0` only removes a floor and
cannot shrink a column that already fits.

---

## Verification

Measured in a real browser, light and dark:

- **0 contrast failures** across `/chat`, `/dashboard`, `/login` × both themes,
  sampled from composited pixels rather than declared values.
- **0 horizontal overflow** at **390 / 430 / 768 / 820 / 1024 / 1440**. The three
  widths this list originally covered (390 / 820 / 1440) were not enough — see the
  drawer section above, where 820 was the only one of them that failed and the
  reason the rule moved out of the mobile media query.
- All ten dashboard sections drive the mobile drawer correctly at 390px.
- All 12 sampled tab stops show a visible focus ring.
- Reduced motion: 0 elements left mid-flight.
- A real streamed answer renders its markdown table and ordered list.
- Console clean (0 errors).
- Test suite: **22/22 suites, 1,392 checks passing** — the template edits broke
  nothing. One run of `run_tests.py` reported `test_announcements` as FAIL; it
  passes standalone (44/44, exit 0) and 22/22 on three consecutive full runs both
  with and without these changes applied, so it is flaky in the runner and not
  caused by this work. Worth knowing about rather than trusting a single green
  run.

`tools/ui_preview.py` serves the three pages with stubbed data for this kind of
inspection; the analytics stub is built from the real `overview()` shape so the
dashboard's JS does not silently take a different branch than it would in
production.
