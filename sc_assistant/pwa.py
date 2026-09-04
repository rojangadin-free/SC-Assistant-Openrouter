"""
sc_assistant/pwa.py — installable app: manifest, service worker, offline page.

Why bother
----------
Almost every student reaches this on a phone, on mobile data, and the flow they
actually use is: open Messenger -> tap the link -> wait for a cold Flask boot ->
ask one question -> close it. Next time, repeat, including the wait. There is no
icon anywhere to come back to, so the assistant is only ever found by someone
sending the link again.

Installing changes three things that matter here:
  1. An icon on the home screen — the app becomes findable without the link.
  2. The shell (CSS/JS/logo) is served from disk, so a cold open on 3G paints
     immediately instead of re-downloading Font Awesome and jQuery every time.
  3. Losing signal produces a page that says so, instead of Chrome's dinosaur.

Why the service worker is served from a ROUTE and not /static
-------------------------------------------------------------
A service worker's *scope* is limited to its own directory and below. At
`/static/js/sw.js` its scope is `/static/js/`, which controls nothing — not `/`,
not `/chat`. It would register successfully, report as active, and never
intercept a single navigation. Serving the identical file from `/sw.js` gives it
root scope. This is the single most common way a PWA is silently broken.

What is deliberately NOT cached
-------------------------------
Nothing under `/chat`, `/api`, `/admin`, `/auth`. An assistant that answers from
a stale cache is worse than one that says it is offline: the whole point of the
conflict/freshness work is that an out-of-date answer looks exactly as
authoritative as a current one. Announcements are the sharpest case — a cached
"classes suspended" from yesterday is actively harmful. So the network is the
only source for anything that carries an answer, and the cache holds only the
shell that draws the UI.
"""

from flask import Blueprint, Response, render_template, url_for

bp_pwa = Blueprint("pwa", __name__)


# The cache name. Bumping it drops the old cache wholesale on the next activate,
# which is the blunt instrument for "ship everything again". Left un-automated on
# purpose: tying it to a timestamp would invalidate the cache on every boot and
# cost every user a full re-download for nothing.
#
# v2, because v1 cached the shell CACHE-FIRST with no revalidation. That was a
# trap: an edited voice.js or main.css was invisible to any browser that had
# already cached it, forever, unless someone remembered to bump this string. It
# cost an afternoon of "the fix is not working" when the fix was fine and the
# browser was serving a copy from June. The fetch handler below now revalidates,
# so this constant is no longer load-bearing for correctness.
#
# v3 drops caches written by v2, which may hold a `/files` response from before
# that path was added to NEVER_CACHE. The new worker would never read it — the
# prefix check short-circuits ahead of caches.match — but leaving a stale copy of
# the document list sitting in storage on every admin's browser serves no purpose.
# v4 ships the rewritten SSE parser in chat.js. Stale-while-revalidate would
# otherwise serve the OLD parser from cache on the first load after this deploy
# and only pick up the new one on the load after that — which, for a bug whose
# entire symptom is "the deployed app behaves differently", is the one failure
# mode guaranteed to be mistaken for the fix not working. Bumping the version
# deletes the previous cache on activate, so the stale copy cannot outlive the
# deploy.
CACHE_VERSION = "sc-assistant-v4"





@bp_pwa.route("/manifest.webmanifest")
def manifest():
    """
    The install descriptor. Served from a route rather than a static file so the
    icon URLs go through `url_for` — they must keep working under a subpath or a
    CDN prefix, and a hardcoded "/static/..." would not.
    """
    icon_192 = url_for("static", filename="images/icon-192.png")
    icon_512 = url_for("static", filename="images/icon-512.png")

    data = {
        "name": "Samar College AI-Assistant",
        # 12 characters is roughly what an Android launcher shows before it
        # truncates; "Samar College AI-Assistant" would render as "Samar Col…".
        "short_name": "SC Assistant",
        "description": (
            "Ask about enrollment, tuition, programs and requirements at "
            "Samar College."
        ),
        # Opens the chat, not the dashboard. The reason someone installs this is
        # to ask a question; making them tap once more to reach the composer
        # wastes the only advantage installing bought them.
        "start_url": "/chat",
        "scope": "/",
        # No browser chrome. There is no URL to type and no page to bookmark, and
        # the app's own header already carries navigation.
        "display": "standalone",
        "orientation": "portrait",
        # Matches the login/splash background so the boot does not flash white.
        "background_color": "#ffffff",
        # Tints the Android status bar. The school green, so the phone's own UI
        # continues the app rather than framing it.
        "theme_color": "#0d4503",
        "lang": "en-PH",
        "dir": "ltr",
        "categories": ["education"],
        "icons": [
            # "any" and "maskable" are listed as separate entries rather than one
            # entry with purpose="any maskable": a single combined entry makes
            # the padded, green-backed icon serve as the browser-tab favicon too,
            # where the padding just looks like a mistake.
            {"src": icon_192, "sizes": "192x192", "type": "image/png",
             "purpose": "any"},
            {"src": icon_512, "sizes": "512x512", "type": "image/png",
             "purpose": "any"},
            {"src": icon_192, "sizes": "192x192", "type": "image/png",
             "purpose": "maskable"},
            {"src": icon_512, "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
        # Long-press the home-screen icon. Two entries, because a launcher shows
        # about four and a list of everything is a menu nobody reads.
        "shortcuts": [
            {
                "name": "Ask a question",
                "short_name": "Ask",
                "url": "/chat",
                "icons": [{"src": icon_192, "sizes": "192x192"}],
            },
            {
                "name": "My replies",
                "short_name": "Replies",
                # The one thing worth a shortcut besides asking: a reply from an
                # office may land days later, and this is how it is found again.
                "url": "/chat?replies=1",
                "icons": [{"src": icon_192, "sizes": "192x192"}],
            },
        ],
    }

    # `flask.jsonify` would send application/json. Chrome accepts that, but
    # Safari's install path wants the registered manifest type.
    import json
    return Response(
        json.dumps(data, indent=2),
        mimetype="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# --------------------------------------------------------------------------- #
# Service worker
# --------------------------------------------------------------------------- #
#
# Written as a Python string rather than a .js file so the shell URLs can be
# generated by url_for and CACHE_VERSION cannot drift out of sync with the value
# used elsewhere in this module. It is served with `Service-Worker-Allowed: /`
# and from the root path, which is what gives it scope over every navigation.

_SW_TEMPLATE = """/* Generated by sc_assistant/pwa.py — do not edit in DevTools.
   Cache: %(version)s */
'use strict';

const CACHE = '%(version)s';

/* The shell only: what is needed to DRAW the app, never what it says.
   A cached answer cannot be told apart from a fresh one by the student
   reading it, so nothing that carries content is listed here. */
const SHELL = %(shell)s;

/* Paths that must always hit the network. Listed as prefixes and checked before
   anything else, because a stale answer — especially a stale announcement —
   is worse than a visible failure.

   Note the admin routes that are NOT under /admin. The admin blueprint is
   registered at the root, so '/files', '/upload' and '/dashboard' are real
   top-level paths and the '/admin' prefix below does not cover them. Missing
   them meant the file list was served stale-while-revalidate: after an upload
   the browser painted the previous list from cache, and only the *next* load
   showed the new document. The symptom is "I have to reload the page before my
   file appears", and the reload was doing nothing except giving the background
   revalidation a chance to land. '/upload' also covers '/upload/status/<id>',
   which had the same problem and is worse — cached progress means a bar frozen
   at whatever percentage happened to be cached. */
const NEVER_CACHE = [
  '/chat', '/api/', '/admin', '/auth', '/logout', '/health',
  '/files', '/upload', '/dashboard', '/delete'
];


self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      /* addAll() is atomic: one 404 in the list and the whole install fails,
         leaving the previous worker in place. Individual puts mean a renamed
         asset degrades to a cache miss instead of bricking the update. */
      .then((cache) => Promise.all(
        SHELL.map((url) => cache.add(url).catch(() => null))
      ))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      /* Take over open tabs immediately. Without this the new worker waits for
         every tab to close, and on a phone that can be weeks. */
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  /* Only GET. A POST is a question being asked or a vote being cast; replaying
     one from a cache would duplicate it. */
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  /* Cross-origin (Font Awesome, jQuery, the marked CDN) is left to the browser's
     own HTTP cache. Mirroring it here would mean shipping a stale copy of
     someone else's library with no way to know it had moved on. */
  if (url.origin !== self.location.origin) return;

  if (NEVER_CACHE.some((p) => url.pathname.startsWith(p))) {
    /* Network only — but a failed NAVIGATION gets the offline page, or the
       student sees the browser's error screen and assumes the app is broken
       rather than that their signal dropped. */
    if (req.mode === 'navigate') {
      event.respondWith(fetch(req).catch(() => caches.match('%(offline)s')));
    }
    return;
  }

  /* Everything else is shell: stale-while-revalidate. The cached copy is served
     immediately (fast paint on 3G, which is the whole point), AND the network is
     asked in the background so the next load has the current file.

     It is emphatically NOT plain cache-first. That was the original design and it
     was a trap: an edited voice.js was invisible to any browser that had already
     cached it — permanently, unless someone remembered to bump CACHE_VERSION.
     Editing a file and seeing no change, with the old version still running and
     no error anywhere, is the worst debugging experience this codebase can
     produce, and it burned an afternoon on a fix that had been correct the whole
     time.

     The cost is one background request per asset per load. On a shell this small
     that is cheaper than the confusion it prevents. */
  event.respondWith(
    caches.match(req).then((hit) => {
      const fresh = fetch(req).then((res) => {
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => {
        /* Offline. If there was a hit it has already been returned; this only
           matters for a miss. */
        if (req.mode === 'navigate') return caches.match('%(offline)s');
        return new Response('', { status: 504, statusText: 'Offline' });
      });

      /* A hit short-circuits the wait, but `fresh` is deliberately left running
         — dropping it here is what would make the update never arrive. */
      return hit || fresh;
    })
  );
});

"""


@bp_pwa.route("/sw.js")
def service_worker():
    """
    Served from the ROOT path deliberately — see the module docstring. A worker
    under /static/ has scope /static/ and would intercept nothing.
    """
    shell = [
        url_for("static", filename="css/main.css"),
        url_for("static", filename="css/chat.css"),
        url_for("static", filename="js/main.js"),
        url_for("static", filename="js/chat.js"),
        url_for("static", filename="js/voice.js"),
        url_for("static", filename="images/logo.png"),
        url_for("static", filename="images/favicon.ico"),
        url_for("pwa.offline"),
    ]

    import json
    body = _SW_TEMPLATE % {
        "version": CACHE_VERSION,
        "shell": json.dumps(shell),
        "offline": url_for("pwa.offline"),
    }

    return Response(
        body,
        mimetype="application/javascript",
        headers={
            # Root scope. Without this header the browser rejects a worker that
            # claims a scope broader than its own directory.
            "Service-Worker-Allowed": "/",
            # The worker is the only file that must never be cached: a stale copy
            # means the CACHE_VERSION bump that ships a fix never arrives.
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@bp_pwa.route("/offline")
def offline():
    """
    The page the service worker falls back to. A real route, not a string
    constant, so it is styled by the same CSS as the rest of the app — an offline
    screen that looks like a browser error is indistinguishable from a crash.
    """
    return render_template("offline.html")
