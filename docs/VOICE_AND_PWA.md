# Voice Input & the Installable App

The twelfth and last item. Everything before it improved *what* the assistant
answers. This one is about *reaching* it: a phone, one thumb, campus wifi that
drops in the corridor.

Two problems, one audience:

1. Students type badly on phones. A question typed with one thumb while walking
   is short, misspelled, and often abandoned.
2. The assistant lives at a URL nobody remembers. There is no icon on the home
   screen, and when the connection drops the browser shows its own error page —
   which looks, to a student, exactly like the school's system being down.

---

## Part 1 — Voice input

### The browser half (`sc_assistant/static/js/voice.js`)

A mic button in the composer, using the Web Speech API. No audio ever leaves the
browser for our servers; recognition is the browser's own (on Chrome, Google's).
Nothing to host, nothing to pay for, no audio to store — which also means no
recordings of students to secure.

Three decisions worth keeping:

**The transcript is never auto-submitted.** It is placed in the input box and the
student presses send. Recognisers mishear; auto-sending would turn every mishear
into a logged question, and — when the model then refuses to answer the garbled
version — into a false content-gap report telling the admin to write a document
nobody asked for. `test_pwa.py` asserts the absence of `form.submit()` for this
reason.

**The button removes itself when it cannot work.** Three separate cases, and each
one was found the hard way:

- Firefox and most in-app webviews (Facebook, Messenger — how a lot of students
  open links) have no Web Speech API at all. Cheap to detect: the object is
  missing.
- The page was not loaded from a **secure context**. Chrome *exposes*
  `SpeechRecognition` on `http://192.168.x.x`, so feature detection passes and
  `start()` succeeds — the refusal only arrives later as an error event. Checking
  `window.isSecureContext` up front is the only way to know before the tap.
- **Brave.** It ships the whole Chromium speech API but not the Google Speech
  keys behind it, on purpose — that API works by uploading audio to Google, which
  is the sort of thing Brave exists to prevent. So the object is present,
  permission is granted, the origin is secure, the connection is fine, and every
  attempt fails with a bare `network` error. `navigator.brave.isBrave()` is the
  only reliable way to know.

Either way the button does not exist, because a mic that does nothing when tapped
reads as a broken app. Feature detection, not a user-agent list.

Where dictation actually works:

| Browser | Mic |
|---|:--:|
| Chrome, Edge (desktop + Android) | yes |
| Safari (iOS 14.5+) | yes |
| **Brave** | no — no speech service, button hidden |
| Firefox | no — no API at all |
| Messenger / Facebook in-app webview | no |

Roughly: Chrome-family and Safari yes, everything privacy-hardened no. The
composer still takes typed questions in all of them, so nothing is actually lost —
which is why hiding the button is the right response rather than a warning banner.


**A `network` error does not mean "check your connection."** It is Chrome's
catch-all, and the most common real cause is a voice model missing for the
requested locale — which reports identically to a genuine outage. Taking it
literally produced a message that was visibly false: the page had just loaded
over the very connection it was blaming. Now `en-PH` failing is retried once in
`en-US` (usually invisible to the student), and only if that also fails is
anything said — with the wording chosen by `navigator.onLine`, so a working
connection is never blamed for an unreachable voice service.


**Errors say what to do.** `not-allowed` means the mic permission was blocked, and
the fix is in browser settings — a message that says so is actionable, whereas
"an error occurred" leaves the student tapping a button that will never work.
`no-speech` (silence) just resets quietly; that one is not an error, it is a
student changing their mind.

Locale is `en-PH`, so local place and program names are recognised far better
than under `en-US`.

### The server half (`rag/dictation.py`)

Dictated text is not typed text, and the difference breaks retrieval in ways that
are invisible in the transcript. This module repairs a spoken query *before*
retrieval sees it. Four repairs:

| Spoken | Transcribed | Repaired |
|---|---|---|
| "B-S-I-T" | `b s i t` | `bsit` |
| "um, when is enrollment" | `um when is enrollment` | `when is enrollment` |
| "...enrollment, question mark" | `enrollment question mark` | `enrollment?` |
| "Summer College" | `summer college` | `samar college` |

The acronym repair is the one that matters. `b s i t` yields **zero** usable
search terms — four one-letter tokens, all of which the query expander discards —
so the dictated question retrieves nothing while the typed one retrieves the
program. The test proves the two forms are *not* interchangeable before the
repair and *are* after it, which is the entire point of the module.

Wired into `rag/chain.py` in front of the existing Taglish/Waray expansion, so a
dictated Waray question gets repaired and then translated. The repair is additive
and idempotent: text that needs nothing is returned unchanged, and repairing
twice changes nothing.

**Tests:** `test_dictation.py` — 60 checks.

---

## Part 2 — The installable app (PWA)

`sc_assistant/pwa.py`, three routes.

### `/manifest.webmanifest`

Makes the app installable: home-screen icon, its own window, no address bar.
Served as `application/manifest+json` (Safari's install path rejects
`application/json`).

- `start_url` is `/chat`, not `/`. The reason to install is to ask a question, so
  the icon opens the composer.
- `display: standalone` — no browser chrome. This is what makes it feel like an
  app rather than a bookmark.
- `scope: /` so an in-app navigation to `/admin` stays inside the app window.
- Shortcuts (long-press the icon): **Ask a question** → `/chat`, and **My
  replies** → `/chat?replies=1`. The second one is honoured by `chat.js`, which
  opens the escalation inbox on arrival and then strips the parameter so a
  refresh does not re-open it. An advertised shortcut that silently did nothing
  would be worse than not offering it.

Icons are generated from the existing logo by `make_pwa_icons.py`, in two
purposes:

- `maskable` — padded to ~80%, because Android crops every icon to the launcher's
  shape (circle, squircle, teardrop) and an unpadded crest loses its rim.
- `any` — unpadded, because the browser-tab favicon does *not* crop, and there the
  padding reads as a mistake.

Both are needed. Declaring only one is the common bug, and it looks fine on
whichever surface the developer happened to check.

### `/sw.js` — served from the root, deliberately

The service worker caches the app shell (CSS, JS, logo, offline page) so a repeat
visit paints instantly and a dropped connection still shows the school's page.

Two things about this file are load-bearing and both fail *silently* if wrong:

**It must be served from `/`, not `/static/js/`.** A worker's scope cannot be
broader than its own directory, so a worker at `/static/js/sw.js` controls
`/static/js/` and intercepts nothing. It registers successfully. DevTools reports
it as active. Nothing works. The test asserts `/static/js/sw.js` returns 404
precisely so nobody "tidies" it into the static folder later.

**It must send `Service-Worker-Allowed: /`.** Without that header the browser
rejects the root-scope registration outright.

The worker is served `no-store`, or the `CACHE_VERSION` bump that ships a fix
would be the one file that never arrives.

**What is never cached:** `/chat`, `/api/`, `/admin`, `/auth`. This is the most
important line in the file. Every previous item in this project exists to make
answers *correct and current* — the conflict resolutions, the freshness ordering,
the live announcement window. A cached answer is indistinguishable from a fresh
one to the student reading it, so caching `/chat` would quietly undo all of it. A
stale "classes are suspended" is the worst possible outcome in this app: a
student who stays home on a day with classes.

Only `GET` is intercepted (replaying a `POST` would re-ask a question or re-cast a
vote), and cross-origin requests are left to the browser's own HTTP cache.

### `/offline`

The fallback for a failed navigation. It is a standalone document with inline CSS
and **no** `extends base.html` — the sidebar and header partials read `user` from
the session, and this page is served from the cache with no session behind it. If
it inherited the layout, the fallback itself would fail and the student would get
the browser's error page: exactly what it exists to replace.

It names the Registrar's office, because someone who genuinely cannot get online
still needs somewhere to go, and it reloads itself when the `online` event fires
so the student does not have to work out that they should retry.

### Registration lives in `base.html`

Not in `chat.html`, for two reasons. It is in the shared layout so the manifest is
present on the **login page** — where a shared link lands, and where Chrome
decides whether to offer "Add to Home screen". Advertising the app only on `/chat`
would show the prompt after the student already got their answer.

And it sits *outside* `{% block extra_body %}`, because `chat.html` overrides that
block; a future edit dropping `super()` would silently unregister the worker, with
no symptom until someone's connection failed.

iOS ignores the manifest entirely and needs its own three tags
(`apple-mobile-web-app-capable`, `-title`, `apple-touch-icon`). Without them there
is no installable app on any iPhone — and Safari is how every iPhone user reaches
this. Registration is deferred to the `load` event so it does not compete with the
first paint on a 3G phone.

**Tests:** `test_pwa.py` — 98 checks, on a bare Flask app with the real templates
and static folder (the same pattern as `test_conflicts_api.py`, since
`create_app()` needs live AWS credentials).

---

## Running it

```bash
python tools/make_pwa_icons.py     # once, regenerates icon-192/512 from the logo
python tests/test_dictation.py     # 60 checks
python tests/test_pwa.py           # 98 checks
python run_tests.py          # all 18 suites
```

### If the mic button is missing, or says voice is not supported

Check the browser first — see the support table above. **Brave hides the button by
design**, because it has no speech service; that is not a bug in this app. Chrome
or Edge for dictation.

If the button is missing in Chrome, the console says which check removed it: an
insecure origin, or Brave detection. Both log a reason.

### The secure-context requirement — read this before testing the mic

Voice input and the install prompt both need a **secure context**. Browsers grant
that to `https://` and to `localhost`, and to nothing else:

| How you opened it | Mic | Install prompt |
|---|:--:|:--:|
| `http://localhost:8080` | works | works |
| `http://192.168.0.x:8080` (phone, plain HTTP) | **no button** | no |
| `https://192.168.0.x:8443` (`--https`) | works | works |

The middle row is the one that looks like a bug. `http://localhost` on your PC is
a secure context, so everything works while developing — then the same build
opened on a phone over the LAN has no mic at all. Nothing is broken; the browser
is refusing. `voice.js` logs the reason to the console, and this is exactly what
`--https` is for:

```bash
pip install cryptography     # once; already in requirements.txt
python run.py --https        # prints the https:// URL your phone should use
```

The certificate is self-signed, so the phone warns once — tap **Advanced →
Proceed** and accept it *before* testing, because a rejected certificate is not a
secure context either. Production is behind a real certificate, so none of this
applies there.

On plain HTTP the service worker registration also fails by design; the catch logs
at `debug` level and the app works normally without it.

### If an edit to CSS or JS seems to do nothing

It is the cache, and this cost real hours before it was understood — worth knowing
the shape of it. A static file has **two** caches in front of it: the browser's own
HTTP cache, and our service worker's. The worker originally served the shell
cache-first with no revalidation, so an edited `voice.js` was served from cache
indefinitely, the file on disk was correct, the browser ran a copy from days
earlier, and nothing anywhere reported an error. A correct fix and a wrong fix look
identical under those conditions.

Both layers are now handled: `url_for('static', …)` appends `?v=<mtime>` (a changed
file is a changed URL, which no cache has seen), and the worker revalidates in the
background. So a normal reload is enough.

If you still suspect a stale asset — a worker from before this fix is still
installed, for instance:

- **DevTools → Application → Service Workers → Unregister**, then reload. This is
  the reliable one.
- Or **Application → Storage → Clear site data**.
- Ctrl-Shift-R alone is *not* always enough: a hard reload bypasses the HTTP cache
  but the service worker still answers from its own.



## Where this sits

```
rag/dictation.py                     spoken query -> searchable query
rag/chain.py                         calls it before query expansion
sc_assistant/pwa.py                  manifest, root-scope sw.js, /offline
sc_assistant/templates/offline.html  standalone, no session
sc_assistant/templates/base.html     manifest + iOS tags + SW registration
sc_assistant/static/js/voice.js      mic button
make_pwa_icons.py                    logo -> maskable + any icons
run.py --https                       secure dev server for phone testing
test_dictation.py                    60 checks
test_pwa.py                          98 checks
```




