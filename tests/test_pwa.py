"""
test_pwa.py — the installable app: manifest, service worker scope, offline page.

    python tests/test_pwa.py

Uses Flask's test client, so no browser and no server. Every assertion here is
something a human tester would have to install the app on a phone to notice, and
several of them fail silently in production — a service worker with the wrong
scope registers successfully, reports as active in DevTools, and controls
nothing. That class of bug is exactly what a test is for.

The blueprint is mounted on a bare Flask app, following the same pattern as
test_conflicts_api.py: the real `create_app()` pulls in Pinecone/Bedrock/boto3
and cannot be imported without live AWS credentials. The app is given
sc_assistant's own template and static folders, so the manifest URLs, the icon
files and the offline page are the real ones — only the unrelated blueprints are
absent.
"""

# Make the repo root importable and force the CWD there: this suite lives in
# tests/ but every import and relative path below assumes the repo root.
import _bootstrap  # noqa: F401

import json
import os

from flask import Flask

from sc_assistant.pwa import bp_pwa

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


# The package dir, resolved from the REPO ROOT rather than from this file. This
# suite used to live in the repo root, where `dirname(__file__)` and the root were
# the same directory; after the move to tests/ that assumption pointed at
# tests/sc_assistant, which does not exist.
#
# The failure was worth reading carefully: Flask does not complain about a
# nonexistent static_folder, so the app built fine and the *icon* checks failed
# instead — four 404s and a Pillow "cannot identify image file". Nothing in that
# output mentions a path, and the obvious reading is that make_pwa_icons.py wrote
# corrupt PNGs. The files were correct the whole time; the test was looking in the
# wrong place.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_REPO_ROOT, "sc_assistant")


app = Flask(
    __name__,
    template_folder=os.path.join(_PKG, "templates"),
    static_folder=os.path.join(_PKG, "static"),
)
app.secret_key = "test-only"
app.config["TESTING"] = True
app.register_blueprint(bp_pwa)
client = app.test_client()



# --------------------------------------------------------------------------- #
section("1. Manifest is served and installable")
# --------------------------------------------------------------------------- #

res = client.get("/manifest.webmanifest")
check("200 OK", res.status_code == 200)
# Chrome tolerates application/json; Safari's install path does not.
check("application/manifest+json",
      "application/manifest+json" in res.headers.get("Content-Type", ""))

mf = json.loads(res.data)

# The four fields Chrome requires before it will offer "Install". Missing any one
# of them means no install prompt, with no error anywhere the developer looks.
for field in ("name", "short_name", "start_url", "icons", "display"):
    check(f"has required field '{field}'", field in mf)

check("display is standalone", mf["display"] == "standalone")
# 12 chars is roughly what an Android launcher shows before truncating.
check("short_name fits a launcher label", len(mf["short_name"]) <= 12)
# Opens the composer, not a landing page: the reason to install is to ask.
check("start_url opens the chat", mf["start_url"] == "/chat")
check("scope covers the whole app", mf["scope"] == "/")
check("theme_color is the school green", mf["theme_color"] == "#0d4503")
check("locale is en-PH", mf.get("lang") == "en-PH")


# --------------------------------------------------------------------------- #
section("2. Icons meet the platform minimums")
# --------------------------------------------------------------------------- #

sizes = {i["sizes"] for i in mf["icons"]}
# 192 is the install-prompt minimum; 512 is the splash screen. Chrome rejects a
# manifest whose largest icon is under 144px.
check("192x192 present", "192x192" in sizes)
check("512x512 present", "512x512" in sizes)

purposes = {i.get("purpose") for i in mf["icons"]}
# Android crops every icon to the launcher's shape. Without a maskable entry the
# crest loses its rim; with ONLY a maskable entry the padded version becomes the
# browser-tab favicon, where the padding reads as a mistake. Both are needed.
check("a maskable icon is declared", "maskable" in purposes)
check("an 'any' icon is declared too", "any" in purposes)

# Declared is not the same as present. A 404 icon is a manifest Chrome discards.
for icon in mf["icons"]:
    r = client.get(icon["src"])
    check(f"icon exists: {icon['src']} ({icon.get('purpose')})",
          r.status_code == 200)

# The 192 file must really be 192 — the manifest is a claim, not a measurement,
# and a 111px file labelled 192x192 is rejected at install time.
try:
    import io
    from PIL import Image
    raw = client.get("/static/images/icon-192.png").data
    check("icon-192.png is genuinely 192x192",
          Image.open(io.BytesIO(raw)).size == (192, 192))
    raw = client.get("/static/images/icon-512.png").data
    check("icon-512.png is genuinely 512x512",
          Image.open(io.BytesIO(raw)).size == (512, 512))
except ImportError:
    print("  SKIP  pixel dimensions (Pillow not installed)")


# --------------------------------------------------------------------------- #
section("3. Shortcuts actually go somewhere")
# --------------------------------------------------------------------------- #

shortcuts = mf.get("shortcuts", [])
check("shortcuts are declared", len(shortcuts) >= 1)
urls = {s["url"] for s in shortcuts}
check("'Ask a question' -> /chat", "/chat" in urls)
# Advertised in the manifest, so chat.js must honour it — otherwise the shortcut
# opens a normal chat screen and the promised inbox never appears.
check("'My replies' -> /chat?replies=1", "/chat?replies=1" in urls)

with open("sc_assistant/static/js/chat.js", encoding="utf-8") as fh:
    chat_js = fh.read()
check("chat.js reads the ?replies=1 parameter", "replies" in chat_js
      and "URLSearchParams" in chat_js)


# --------------------------------------------------------------------------- #
section("4. Service worker: scope is the whole point")
# --------------------------------------------------------------------------- #

res = client.get("/sw.js")
check("served from the ROOT path", res.status_code == 200)
check("javascript content type",
      "javascript" in res.headers.get("Content-Type", ""))

# THE critical header. A worker may not claim a scope broader than its own
# directory unless the server says so. Without it, registration with {scope:'/'}
# is rejected outright.
check("Service-Worker-Allowed: /",
      res.headers.get("Service-Worker-Allowed") == "/")

# The worker itself must never be cached, or the CACHE_VERSION bump that ships a
# fix is the one file that never arrives.
cache_control = res.headers.get("Cache-Control", "")
check("worker is not cacheable",
      "no-store" in cache_control or "no-cache" in cache_control)

# A worker under /static/ would have scope /static/ and intercept nothing.
check("NOT served from /static/js/sw.js",
      client.get("/static/js/sw.js").status_code == 404)

sw = res.get_data(as_text=True)


# --------------------------------------------------------------------------- #
section("5. Service worker caches the shell and nothing else")
# --------------------------------------------------------------------------- #

check("registers an install handler", "addEventListener('install'" in sw)
check("registers a fetch handler", "addEventListener('fetch'" in sw)
check("claims open tabs on activate", "clients.claim" in sw)
check("deletes old caches on activate", "caches.delete" in sw)

# The shell: what draws the UI.
for asset in ("css/main.css", "js/chat.js", "js/voice.js", "images/logo.png"):
    check(f"shell includes {asset}", asset in sw)

# And the part that actually matters. A cached answer is indistinguishable from a
# fresh one to the student reading it, which would undo every guarantee the
# conflict, freshness and announcement work provides. A stale "classes
# suspended" is the worst case in the whole app.
check("/chat is never cached", "'/chat'" in sw)
check("/api/ is never cached", "'/api/'" in sw)
check("/admin is never cached", "'/admin'" in sw)
check("/auth is never cached", "'/auth'" in sw)

# Regression, and the expensive one. The shell was originally cache-FIRST with no
# revalidation, which meant an edited voice.js was served from cache forever
# unless someone remembered to bump CACHE_VERSION: the file on disk was right, the
# browser ran a copy from days before, and nothing anywhere reported an error. The
# symptom — "the fix does not work" — is indistinguishable from a wrong fix, and
# that is what makes it worth a test rather than a comment.
check("revalidates the shell in the background",
      "const fresh = fetch(req)" in sw)
# The cached copy must still answer immediately, or the fast paint is lost.
check("still serves the cached copy first", "return hit || fresh" in sw)
# Only GET. Replaying a POST from a cache would re-ask a question or re-cast a
# vote.
check("non-GET requests are ignored", "req.method !== 'GET'" in sw)

# Cross-origin CDNs are left to the browser's own HTTP cache.
check("cross-origin is left alone", "url.origin !== self.location.origin" in sw)
# A failed navigation must reach the offline page, not the browser's dinosaur.
check("falls back to the offline page", "/offline" in sw)

# The second half of the same fix, in create_app: ?v=<mtime> on every static URL,
# so an edited file arrives under a URL no cache has ever seen. Asserted at the
# source level because the hook lives in create_app(), which cannot be imported
# here (boto3/Pinecone). Belt and braces with the revalidation above — this one
# also defeats the *browser's* HTTP cache, which the service worker cannot touch.
with open("sc_assistant/__init__.py", encoding="utf-8") as fh:
    init_py = fh.read()
check("static URLs are cache-busted", "url_defaults" in init_py)
check("cache-buster uses the file mtime", "st_mtime" in init_py)
# Applied globally, not per-template: a hand-written ?v= in one <script> tag is
# the version someone forgets to update.
check("cache-buster covers every template",
      'endpoint != "static"' in init_py)
# A missing file must still 404 honestly rather than 404 with a version on it.
check("cache-buster tolerates a missing file", "except OSError" in init_py)



# --------------------------------------------------------------------------- #
section("6. Offline page stands on its own")
# --------------------------------------------------------------------------- #

res = client.get("/offline")
check("200 OK", res.status_code == 200)
html = res.get_data(as_text=True)

# Served from the CACHE with no session behind it. If it extended base.html the
# sidebar/header partials would look up `user` and the fallback itself would
# fail — leaving exactly the browser error page it exists to replace.
check("does not render the sidebar", 'id="sidebar"' not in html)
check("is a complete document", "<!DOCTYPE html>" in html)
# Inline CSS: the page whose only job is to work when nothing else does must not
# depend on a stylesheet fetch.
check("styles are inline", "<style>" in html)
check("says what is wrong", "offline" in html.lower())
# A way back, and a human fallback for someone who cannot get online at all.
check("offers a retry", "/chat" in html)
check("names a real office", "Registrar" in html)
check("auto-reloads when the connection returns", "'online'" in html)


# --------------------------------------------------------------------------- #
section("7. Every page advertises the app")
# --------------------------------------------------------------------------- #

# base.html is asserted at the SOURCE level rather than by fetching a page.
# Rendering it needs `user` in the session (the sidebar and header partials read
# it), and the property being defended is not "one page has these tags" but "the
# shared layout does" — which is what puts them on the login page, where a shared
# link lands and where Chrome decides whether to offer "Add to Home screen".
# Advertising the app only on /chat would show the prompt after the student
# already got their answer.
with open("sc_assistant/templates/base.html", encoding="utf-8") as fh:
    base_html = fh.read()

check("shared layout links the manifest", 'rel="manifest"' in base_html)
check("shared layout sets theme-color", 'name="theme-color"' in base_html)
# iOS ignores the manifest completely; without these tags there is no installable
# app on any iPhone, and Safari is how every iPhone user reaches this.
check("iOS: apple-mobile-web-app-capable",
      "apple-mobile-web-app-capable" in base_html)
check("iOS: apple-touch-icon", 'rel="apple-touch-icon"' in base_html)
check("registers the service worker", "serviceWorker" in base_html)
# Must point at the ROOT worker. A /static path would register successfully and
# control nothing.
check("registration targets pwa.service_worker",
      "pwa.service_worker" in base_html)
check("registration asks for root scope", "scope: '/'" in base_html)
# After load, so it does not compete with the first paint on a slow connection.
check("registration waits for load", "'load'" in base_html)
# Outside {% block extra_body %}, or chat.html's override could silently drop it —
# a failure whose only symptom appears when the connection does.
check("registration is not inside an overridable block",
      base_html.index("serviceWorker") < base_html.index("block extra_body"))

# And the routes it references must resolve on the real blueprint.
with app.test_request_context():
    from flask import url_for
    check("url_for('pwa.manifest') resolves",
          url_for("pwa.manifest") == "/manifest.webmanifest")
    check("url_for('pwa.service_worker') resolves",
          url_for("pwa.service_worker") == "/sw.js")



# --------------------------------------------------------------------------- #
section("8. Voice input is wired into the composer")
# --------------------------------------------------------------------------- #

with open("sc_assistant/templates/chat.html", encoding="utf-8") as fh:
    chat_html = fh.read()

check("mic button exists", 'id="micBtn"' in chat_html)
check("mic button is not a submit", 'type="button"' in chat_html)
check("mic button is labelled for screen readers", 'aria-label="Ask by voice"' in chat_html)
check("voice.js is loaded", "js/voice.js" in chat_html)
# voice.js re-fires the 'input' event that chat.js binds for auto-grow, so the
# order is load-bearing.
check("voice.js loads AFTER chat.js",
      chat_html.index("js/voice.js") > chat_html.index("js/chat.js"))

res = client.get("/static/js/voice.js")
check("voice.js is served", res.status_code == 200)
voice_js = res.get_data(as_text=True)

check("uses the Philippine English locale", "en-PH" in voice_js)
check("supports the webkit-prefixed API", "webkitSpeechRecognition" in voice_js)
# A mic that cannot listen reads as a broken app, so the button is removed
# outright on Firefox and in-app webviews.
check("removes the button when unsupported", "btn.remove()" in voice_js)

# Regression: over plain http:// the API OBJECT exists, so feature detection
# passes, start() succeeds, and the only symptom is an error event that used to be
# reported as "Voice needs a connection" — on a page that had just loaded over
# that very connection. The origin has to be checked up front.
check("checks for a secure context", "isSecureContext" in voice_js)
check("removes the button on an insecure origin",
      voice_js.count("btn.remove()") >= 2)
# Whoever is testing over the LAN needs to know WHY the button vanished.
check("explains the missing button in the console",
      "requires https" in voice_js)

# A bare 'network' error is Chrome's catch-all and is also what a missing voice
# model for the locale reports. Retrying in en-US recovers those users silently
# instead of telling them to fix their internet.
check("declares a fallback locale", "en-US" in voice_js)
check("retries once before reporting", "retried" in voice_js)
# ...and only once, or a dead mic loops quietly forever.
check("the retry is latched", "retried = true" in voice_js)
# Never blame the connection while the browser says it is online.
check("distinguishes an outage from an unavailable service",
      "navigator.onLine" in voice_js)

# Brave. It ships Chromium's SpeechRecognition object but not the Google speech
# service behind it, so every attempt fails with 'network' while permission is
# granted, the origin is secure and the connection is fine — the exact state that
# looks like an app bug and is not. This was the real cause of the reported
# failure, after the secure-context and locale theories were both ruled out.
check("detects Brave", "navigator.brave" in voice_js)
check("Brave detection uses isBrave()", "isBrave()" in voice_js)
check("removes the button on Brave", voice_js.count("btn.remove()") >= 3)
check("names a working browser in the console",
      "Use Chrome or Edge" in voice_js)
# isBrave() is a promise; a missing .catch would throw on non-Brave Chromium.
check("Brave check cannot throw elsewhere",
      "isBrave().then" in voice_js and ".catch(" in voice_js)

# The last-resort message must name the actual remedy. "Unavailable" is true and
# useless — a student cannot guess that the fix is a different browser, and the
# privacy forks isBrave() cannot detect land here.
check("final message names the remedy",
      "try Chrome" in voice_js)
check("does not blame a working connection",
      "Voice needs a connection" in voice_js
      and voice_js.index("navigator.onLine")
          < voice_js.index("Voice needs a connection"))


# Auto-sending a misheard question turns a recogniser error into a logged
# question and, on a refusal, a false content-gap report.
check("does NOT auto-submit the transcript",
      "form.submit()" not in voice_js and ".submit();" not in voice_js)
check("explains a blocked microphone", "not-allowed" in voice_js)
check("handles silence", "no-speech" in voice_js)


# --------------------------------------------------------------------------- #
print(f"\n{'='*54}\n  {_passed} passed, {_failed} failed\n{'='*54}")
raise SystemExit(1 if _failed else 0)
