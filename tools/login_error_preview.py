"""Render the login page against the REAL /login route, so the error toast can
be reviewed as a student sees it.

tools/ui_preview.py serves the templates with every backend faked, and its
catch-all answers POST /login with `{"success": true}` — the front-end then
redirects to `url_for(...)` = undefined, which is why the browser ends up at
/undefined and no toast is ever shown. That harness is for looking at layout, not
for exercising auth.

This mounts the actual sc_assistant.auth blueprint, so POST /login runs
login_user() -> Cognito -> handle_cognito_error() and returns the real JSON the
toast is built from. Only the templates and static files are shared with the app;
nothing here is imported by production.

    python tools/login_error_preview.py   ->  http://localhost:5056/auth
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, render_template  # noqa: E402

from sc_assistant.auth import bp as auth_bp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "sc_assistant")

app = Flask(
    __name__,
    template_folder=os.path.join(PKG, "templates"),
    static_folder=os.path.join(PKG, "static"),
)
app.secret_key = "login-error-preview"
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


@app.url_defaults
def _static_cache_bust(endpoint, values):
    if endpoint != "static" or "filename" not in values:
        return
    try:
        values["v"] = int(
            os.stat(os.path.join(app.static_folder, values["filename"])).st_mtime
        )
    except OSError:
        pass


app.register_blueprint(auth_bp)


# auth.py's success path calls url_for("chat.chat_page") / url_for("admin.dashboard"),
# so those endpoints have to exist for the blueprint to be registrable — but they
# are never reached while sign-in is failing, which is the case under review.
@app.route("/chat", endpoint="chat.chat_page")
@app.route("/", endpoint="chat.index")
def chat_page():
    return "signed in"


@app.route("/dashboard", endpoint="admin.dashboard")
def dashboard():
    return "admin dashboard"


@app.route("/settings", endpoint="settings.settings_page")
def settings_page():
    return "settings"


@app.route("/manifest.webmanifest", endpoint="pwa.manifest")
def manifest():
    return jsonify({"name": "SC Assistant"})


@app.route("/sw.js", endpoint="pwa.service_worker")
def service_worker():
    return "/* preview */", 200, {"Content-Type": "application/javascript"}


@app.route("/offline", endpoint="pwa.offline")
def offline():
    return render_template("offline.html")


if __name__ == "__main__":
    print("\n  Login error preview (REAL Cognito calls):")
    print("    http://localhost:5056/auth\n")
    app.run(host="127.0.0.1", port=5056, debug=False, threaded=True)
