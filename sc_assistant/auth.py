from flask import (
    Blueprint, render_template, jsonify, request, session, redirect, url_for
)
from jose import jwt
from config import COGNITO_USER_POOL_ID
from aws.cognito import (
    sign_up_user, login_user, forgot_password, reset_password,
    get_user_role_from_claims, cognito_client
)
from .utils import is_admin

# Create a Blueprint
bp = Blueprint('auth', __name__)

@bp.route("/auth")
def auth_page():
    if session.get("user"):
        return redirect(url_for("chat.index"))
    return render_template("auth.html")

@bp.route("/signup", methods=["POST"])
def signup():
    email = request.form.get("email")
    password = request.form.get("password")
    username = request.form.get("username")

    if not all([email, password, username]):
        return jsonify({"success": False, "message": "All fields are required."})

    result = sign_up_user(username, email, password)
    if result["success"]:
        session.update({
            "user": email, "uid": result.get("user_sub"),
            "username": username, "role": "user"
        })
        # Note: url_for uses 'blueprint_name.function_name'
        return jsonify({"success": True, "redirect": url_for("chat.chat_page")})
    return jsonify(result)

@bp.route("/login", methods=["POST"])
def login():
    identifier = request.form.get("email")
    password = request.form.get("password")

    if not all([identifier, password]):
        return jsonify({"success": False, "message": "Both identifier and password are required."})

    result = login_user(identifier, password)
    if result["success"]:
        auth_result = result["auth_result"]
        id_token = auth_result["IdToken"]
        claims = jwt.get_unverified_claims(id_token)
        
        # Store both email and the actual Cognito username
        session.update({
            "user": identifier, 
            "uid": claims["sub"], 
            "id_token": id_token,
            "username": claims.get("name", identifier.split("@")[0]),
            "cognito_username": claims.get("cognito:username", identifier),  # Store actual Cognito username
            "role": get_user_role_from_claims(id_token)
        })
        session['start_new_chat'] = True
        redirect_url = url_for("admin.dashboard") if is_admin() else url_for("chat.chat_page")
        return jsonify({"success": True, "redirect": redirect_url})
    return jsonify(result)

@bp.route("/forgot-password", methods=["POST"])
def handle_forgot_password():
    email = request.form.get("email")
    if not email:
        return jsonify({"success": False, "message": "An email address is required."})
    return jsonify(forgot_password(email))

@bp.route("/reset-password", methods=["POST"])
def handle_reset_password():
    email = request.form.get("email")
    code = request.form.get("code")
    new_password = request.form.get("password")

    if not all([email, code, new_password]):
        return jsonify({"success": False, "message": "Email, code, and new password are required."})
    return jsonify(reset_password(email, code, new_password))

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.auth_page"))