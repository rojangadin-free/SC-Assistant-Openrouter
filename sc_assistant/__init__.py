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
            from rag.reranker import warmup
            warmup()
        except Exception as e:
            # Deliberately swallowed. A cold reranker answers slower; a crashed
            # startup thread answers nothing.
            print(f"[startup] Reranker warmup skipped: {e}")

    threading.Thread(target=_warm_reranker, daemon=True).start()









    return app