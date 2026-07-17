from flask import Blueprint, jsonify, request, session, render_template, redirect, url_for
from .utils import is_admin
from aws.dynamodb import list_reports, update_report_status, get_conversation

bp_reports = Blueprint('reports', __name__, url_prefix='/admin/reports')


def _require_admin():
    if not session.get("user") or not is_admin():
        return False
    return True


@bp_reports.route("/")
def reports_page():
    if not _require_admin():
        return redirect(url_for("auth.auth_page"))
    user_obj = {
        "email":    session.get("user"),
        "username": session.get("username", session.get("user", "").split("@")[0]),
    }
    return render_template("admin_reports.html", user=user_obj)


@bp_reports.route("/api/list")
def api_list():
    if not _require_admin():
        return jsonify({"error": "Unauthorized"}), 403
    status_filter = request.args.get("status")  # pending | done | ignored | None=all
    reports = list_reports(status_filter=status_filter if status_filter != "all" else None)
    return jsonify(reports)


@bp_reports.route("/api/resolve", methods=["POST"])
def api_resolve():
    if not _require_admin():
        return jsonify({"error": "Unauthorized"}), 403
    data      = request.get_json()
    report_id = data.get("report_id")
    status    = data.get("status")  # "done" or "ignored"
    if not report_id or status not in ("done", "ignored"):
        return jsonify({"error": "Invalid request"}), 400
    update_report_status(report_id, status)
    return jsonify({"status": "ok"})


@bp_reports.route("/api/conversation/<reporter_uid>/<conv_id>")
def api_get_reported_conversation(reporter_uid, conv_id):
    if not _require_admin():
        return jsonify({"error": "Unauthorized"}), 403
    
    conv = get_conversation(reporter_uid, conv_id)
    if not conv:
        return jsonify({"error": "Conversation context not found"}), 404
        
    return jsonify(conv)