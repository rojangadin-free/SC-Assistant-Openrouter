import os

from flask import Flask
from config import FLASK_SECRET_KEY

def create_app():
    """
    Application factory to create and configure the Flask app.
    """
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.secret_key = FLASK_SECRET_KEY

    # ----------------------------------------------------------------------- #
    # Static asset cache-busting
    # ----------------------------------------------------------------------- #
    # Appends ?v=<mtime> to every url_for('static', ...) URL, so editing a file
    # changes its URL and no cache anywhere can serve the old bytes.
    #
    # This is not premature optimisation, it is a bug fix. Two layers cache these
    # files: the browser's HTTP cache and our own service worker. An edit to
    # voice.js was invisible through both — the file on disk was correct, the
    # browser ran a copy from days earlier, and nothing reported an error. The
    # only symptom was a fix that "did not work", which is indistinguishable from
    # a fix that is wrong. Hours went into the wrong question because of it.
    #
    # Applied via url_defaults so it covers every template, including ones added
    # later; doing it by hand in each <script> tag guarantees someone forgets.
    @app.url_defaults
    def _static_cache_bust(endpoint, values):
        if endpoint != "static" or "filename" not in values:
            return
        # os.stat on every asset reference is a stat() of an already-hot inode —
        # nothing measurable next to rendering the page, and correctness here is
        # worth more than the microsecond.
        try:
            path = os.path.join(app.static_folder, values["filename"])
            values["v"] = int(os.stat(path).st_mtime)
        except OSError:
            # Missing file: leave the URL clean and let the 404 speak for itself
            # rather than masking it with a bogus version.
            pass

    @app.route('/health')
    def health_check():
        return "OK", 200

    # ----------------------------------------------------------------------- #
    # Say where admin state is going, at boot
    # ----------------------------------------------------------------------- #
    # The whole shared-storage layer is invisible when it is misconfigured: on
    # `file` the dashboard works perfectly, saves successfully, and simply keeps
    # the data somewhere no other instance — and no future container — will ever
    # look. That is how a calendar period saved on one machine came to be missing
    # on the deployed site with no error anywhere.
    #
    # `.env` is not in the image (gitignored, and secrets arrive as `-e` flags), so
    # STORE_BACKEND was unset in production and silently defaulted to `file`.
    # config.py now defaults it to `dynamodb`; this line makes the actual choice
    # visible in the container logs, so the next misconfiguration is one `docker
    # logs` away instead of a UI mystery.
    try:
        from rag.store import describe_backend

        info = describe_backend()
        if info["shared_across_instances"]:
            print(f"[startup] Admin state -> DynamoDB table '{info['table']}' (shared)")
        else:
            print(
                "[startup] WARNING: admin state -> local JSON files (NOT shared).\n"
                "           Calendar periods, announcements, conflict decisions and\n"
                "           document dates will be invisible to other instances and\n"
                "           lost on the next redeploy.\n"
                "           fix   : set STORE_BACKEND=dynamodb"
            )
    except Exception as e:
        print(f"[startup] Could not determine storage backend: {e}")



    with app.app_context():
        from . import auth
        app.register_blueprint(auth.bp)

        from . import chat
        app.register_blueprint(chat.bp)

        from . import admin
        app.register_blueprint(admin.bp)

        from . import settings
        app.register_blueprint(settings.bp)

        from .admin_reports import bp_reports
        app.register_blueprint(bp_reports)

        from .admin_conflicts import bp_conflicts
        app.register_blueprint(bp_conflicts)

        from .admin_gaps import bp_gaps
        app.register_blueprint(bp_gaps)

        from .admin_feedback import bp_feedback
        app.register_blueprint(bp_feedback)

        from .admin_calendar import bp_calendar
        app.register_blueprint(bp_calendar)

        from .admin_announcements import bp_announcements
        app.register_blueprint(bp_announcements)

        from .admin_analytics import bp_analytics
        app.register_blueprint(bp_analytics)

        from .admin_freshness import bp_freshness
        app.register_blueprint(bp_freshness)

        # Installable-app plumbing: /manifest.webmanifest, /sw.js, /offline.
        # No url_prefix, deliberately — a service worker's scope is its own
        # directory, so anything but the root path would leave it unable to
        # intercept /chat. See sc_assistant/pwa.py.
        from .pwa import bp_pwa
        app.register_blueprint(bp_pwa)

    # ----------------------------------------------------------------------- #
    # Warm the cross-encoder before the first question arrives
    # ----------------------------------------------------------------------- #
    # `rag/reranker.py` loads its model lazily on first use, which means the
    # load — several seconds of reading weights off disk — happened *inside* the
    # first student's request after every deploy and every worker restart. They
    # paid for it and saw nothing but a blank screen; the second student got the
    # cached model and a fast answer, which is exactly the pattern that makes
    # "sometimes it's slow" impossible to reproduce.
    #
    # `warmup()` already existed for this and was called from nowhere. It is
    # called on a background thread so a slow or failed load cannot delay the
    # server accepting connections: on failure the reranker degrades to the
    # hybrid retriever's own ordering, which is the same behaviour as before and
    # not worth blocking a boot over. /health must answer immediately.
    import threading

    def _warm_reranker():
        try:
            from rag.reranker import (
                RERANKER_MODEL_NAME,
                disabled,
                unavailable_reason,
                warmup,
            )

            # Off on purpose. Reported as information, not as a warning with a
            # fix, because there is nothing to fix — and reported at all because
            # a deliberate RERANKER_ENABLED=false and a broken model produce
            # byte-identical behaviour (retrieval order, no scores). Anyone
            # debugging "why are the answers worse than the demo" needs to be
            # able to tell those two apart from the boot output alone.
            if disabled():
                print(
                    "[startup] Reranker DISABLED (RERANKER_ENABLED=false) — answers "
                    "keep hybrid retrieval order and carry no score.\n"
                    "           capacity : higher (no CPU-bound phase, torch not loaded)\n"
                    "           quality  : ordering within the top-K is unranked\n"
                    "           re-enable: unset RERANKER_ENABLED, see docs/CAPACITY.md"
                )
                return

            if warmup():
                print(f"[startup] Reranker ready: {RERANKER_MODEL_NAME}")
                return


            # warmup()'s return value used to be discarded, which is how a
            # deployment could run for weeks with the reranker off: the only
            # trace was `score=n/a` on every document in the retrieval log,
            # and answers silently kept raw hybrid-retrieval order. Say it once,
            # loudly, at the only moment someone is reading the boot output.
            print(
                "[startup] WARNING: reranker OFF — answers will keep raw retrieval "
                f"order and no document will carry a score.\n"
                f"           model : {RERANKER_MODEL_NAME}\n"
                f"           cause : {unavailable_reason()}\n"
                "           fix   : python download_model.py"
            )
        except Exception as e:
            # Deliberately swallowed. A cold reranker answers slower; a crashed
            # startup thread answers nothing.
            print(f"[startup] Reranker warmup skipped: {e}")


    threading.Thread(target=_warm_reranker, daemon=True).start()









    return app